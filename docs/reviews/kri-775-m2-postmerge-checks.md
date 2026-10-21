# Merope M2 post-merge assertion reconciliation

## Scope and acceptance

KRI-775, after #1734, #1708, #1709, #1711, #1713, #1716, #1722 and #1710
merged. Main CI on `c58b89bbdf54afd8db8bf6dca15bc6eee5e59559` failed seven
tests. The isolated repair starts at fresh main
`110e87197ccaef219c5dfcfdd65f5f7b41dd712a`.

Before editing, acceptance was recorded in `.dev/acceptance.md`: preserve the
original total-length check, the explicit unsupported-freeze explanation, six
window checks, and honest approval copy. Do not reinterpret an unsupported
freeze as a video-length request. This is test integration, not a product fix or
proof that the six depicted actions satisfy the request.

## Failure evidence and changes

[Combined main CI](https://github.com/emirerben/nova/actions/runs/39660107296)
reported:

| Test | Merged behavior and retained expectation |
| --- | --- |
| M2 replay freeze sentinel | Its original assertion now passes with KRI-777. Remove only its strict xfail marker; the assertion is unchanged. |
| KRI-778 total-length sentence, four EN/TR and outro cases | The message now includes the truthful freeze limitation before the one total-length sentence. Require both sentences, with exact localized copy and the original duration arithmetic. |
| KRI-776 draft and approval card | Both now use the checked receipt summary, stating freeze could not be done and six windows were planned. Require the same honest summary in the card; do not require the old model promise to overwrite it. |
| Dispatch checked-miss statuses | Freeze is `not_possible`, not `partial`. Order and total-length misses stay `partial`; the worker's recovery copy remains asserted exactly. |

All seven were reproduced before changes using the real receipt/runtime code
and scratch Postgres: six failures in the non-DB run, plus the draft/card
failure in the DB run. The initial reproduction used the policy worktree at
`9564fe0bc8befc5457c1ae9212f4a7c4cb4f11ce`, whose product and M2 test files
match the isolated base above. #1734's DB-backed KRI-705 regression passed.
The corrected card expectation was separately checked after the first failure
uncovered its old model-summary assertion.

The original regression tests and product changes are Yasin's; this change
reconciles their combined expectations. No product file changes. No new skip,
xfail, weakened duration assertion, or blanket suppression is introduced.

## Validation

Scratch database: `bash scripts/dev/test-db.sh` (isolated database for this worktree).

```sh
bash scripts/check-api.sh test \
  tests/kria/test_kri775_merope_m2_replay.py \
  tests/kria/test_kri776_moment_ask_in_brief.py \
  tests/kria/test_kri778_block_message.py \
  tests/tasks/test_unified_montage_dispatch.py \
  tests/kria/test_kri777_freeze_frame_ask.py \
  tests/tasks/test_kri777_freeze_render.py \
  tests/kria/test_kri796_photo_between_moments.py \
  tests/gates/test_reliability_gates.py -q --tb=short
```

Result: **480 passed, 3 xfailed**. Scoped Ruff lint/format and whitespace checks
passed. Hosted CI must independently validate the final committed head.

## Limits

The other three M2 expected failures remain: both recorded renders' six-window
gate assertions, and the compound draft-buildability case. Source grounding,
the original compound request, recovery and actual phone export remain KRI-775
and KRI-723 work. Nothing here proves that a chronological window contains its
named action. No phone operation, paid model call, deployment, held T1 merge or
repository-ruleset change was performed.
