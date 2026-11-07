# Suggestion Undo: shared-test database isolation

## Incident and acceptance

Main CI [42319180028](https://github.com/emirerben/nova/actions/runs/42319180028)
failed the Talking case of
`test_v1_v2_every_card_served_after_a_save_executes` after #2143. A native Save,
hook suggestion tap and Undo left the hook's brief requirement live:
“Whisk first and talk later”. The pre-tap brief was empty.

The existing test's expected outcome is unchanged: Undo restores the pre-tap draft,
live brief and settings. This repair changes only test setup, not application Undo,
receipts, approval readiness, native code or production settings.

Acceptance recorded before the fix:
- G8 must still simulate an approval not staged by a card and prove that denying
  it preserves its own brief, item flags and consumed-card record.
- A different thread can be between Save/hook tap and Undo during that setup.
  Its execution result must remain identical, and its real Undo must restore the
  exact earlier draft and live brief.
- No retry, skip, timeout increase or weaker existing assertion is permitted.

## Root cause

`test_g8_an_approval_no_card_staged_is_denied_as_before` selected every
`CreatorAgentExecution` row and removed each `pinned_undo` entry. Parallel pytest
workers share Postgres, so this erased another test's legitimate Undo evidence.
The production withdrawal function correctly does nothing without that footprint.

The failed CI log establishes the overlap:
- gw2 started the failing Talking Save case at 00:55:41.657 UTC.
- gw0 ran G8 at 00:55:42.336–00:55:43.472 UTC.

The broad mutation originated in #2109, commit
`08948053719ed8bd0a4721c3a3e598cef2c2c7a0`. It is unchanged on the pre-#2142
baseline `b9177a6a462819f33bdae1995cdc348eb05b3b2f`. Neither #2142 nor #2143
introduced it.

The fix scopes G8's setup to the exact accepted tap turn ID. The strengthened G8
stages the neighboring Save/hook before that mutation and performs its Undo after,
forcing the same interleaving without timing or scheduling assumptions. It also
compares the neighboring execution's complete stored JSON result before and after.
All original G8 denial assertions remain intact.

Failure modes considered: an unfiltered/global query, selecting a global latest
execution, confusing the two threads or turns, failing to strip the owned
footprint, and weakening the original Undo expectation. The latter would conceal
a real regression and is explicitly not part of this repair.

## Evidence scope and reproduction

Offline replay uses migrated, worktree-specific Postgres databases and the real
Save route, suggestion planning/commit and Undo boundary. Existing harnesses replay
provider transport and poison paid model and broker calls. No live model, native
export or production user-flow claim is made. Language parsing is unchanged;
the neighboring suite retains its EN/TR cases.

Original incident node:

```sh
bash scripts/check-api.sh test 'tests/e2e_suggestions/test_r5_save.py::test_v1_v2_every_card_served_after_a_save_executes[nermin-matcha-update:talking]' -q
```

Deterministic pollution probe:

```sh
bash scripts/check-api.sh test tests/tasks/test_kria_pinned_r4_withdraw.py::test_g8_an_approval_no_card_staged_is_denied_as_before -q
```

For the negative control, run the strengthened G8 against the pre-#2142 baseline
with only its mutation query restored to the original unfiltered select. The
neighbor's execution evidence and live brief assertions reject that corruption.
This is an isolation check, not evidence of semantic video fulfillment.

## Results

- Original victim alone: 0 failures / 10 runs on current main `502ecbe47`, and
  0 failures / 10 runs on pre-#2142 baseline `b9177a6a`.
- Forced interleaving before the fix: 10 failures / 10 runs on the pre-#2142
  baseline. The final test with the original broad query also fails 10/10,
  rejecting the changed neighboring execution result. Before adding that explicit
  result equality, the same replay failed 10/10 on the exact leftover hook brief.
- Final scoped fixture: 0 failures / 10 forced runs, plus its direct focused run.
- Four neighboring suites in four parallel workers: 94 passed, zero skipped,
  rerun after the additional execution-result equality assertion (45.33 seconds).

These are diagnostic samples and deliberate controls, not automatic retries used
to turn a red result green. The final guard rejects the unscoped-mutation
regression in 10/10 forced runs, while the fixture's expected behavior passes in
10/10. No assertion was loosened. The final full suggestions and pinned-tap suite passes **1,134 tests**, zero skipped,
in four parallel workers (263.90 seconds). This rerun includes the explicit
execution-result equality assertion.

Final neighboring command:

```sh
bash scripts/check-api.sh test tests/tasks/test_kria_pinned_r4_withdraw.py tests/e2e_suggestions/test_r5_save.py tests/tasks/test_kria_pinned_undo.py tests/e2e_suggestions/test_r5_lifecycle.py -q -n 4
```

Broader command:

```sh
bash scripts/check-api.sh test tests/e2e_suggestions tests/tasks/test_kria_pinned_r4_withdraw.py tests/tasks/test_kria_pinned_undo.py tests/tasks/test_kria_pinned_suggestion.py tests/tasks/test_kria_pinned_kinds.py tests/tasks/test_kria_pinned_r5_lifecycle.py -q -n 4
```

Scoped Ruff, formatting, diff checks and the instruction-size guard pass. The
reliability selector maps no gate to this test-only path; the explicit suites
above cover it instead. Deprecation warnings come from slowapi on local Python
3.14. CI uses Python 3.11; its result is reported separately on the PR.
