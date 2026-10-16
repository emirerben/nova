# Automatic request-following loop: preregistered failures

Scope: scoped, consented metadata capture; human intent review; private export into
existing incident tooling; scheduled deterministic regression reports. No provider
calls, prompt changes, automatic ground truth, or claims of rendered-output proof.

Before implementation, the acceptance journey must fail if:
- disabled/empty scope captures anything, or scope widens through malformed UUIDs;
- an unconsented creator, deleted thread, or another creator's linked item is captured;
- repeated collection duplicates unchanged evidence, concurrent collectors race,
  or the persisted sweep cursor loses sessions/late execution updates;
- source changes preserve approval, a stale reviewer overwrites newer intent,
  or exports use stale source content;
- revoke/re-grant revives an old capture, expiry leaves transcript accessible,
  or account/thread deletion leaves retained private content;
- held-out sessions leak request bodies or can become development examples;
- expectations cite assistant messages or missing source events;
- a failed turn or ready job is presented as an intent/output verdict;
- exported intent review skips redaction/incident review in the existing CLI;
- the daily workflow calls providers, drops failures, or reports recorded evidence
  as live understanding or rendered-video verification.

Verification: migrated disposable PostgreSQL, task entrypoint -> admin HTTP list,
detail, review, export -> CLI import -> existing incident tooling. Exercise scope,
consent, deduplication, cursor wrap, stale source/review, holdout, expiry and deletion.
Run existing CLI/corpus tests and the offline report runner. Inspect admin UI with
keyboard and narrow viewport; preserve loading/empty/error states. Record actual
commands/results separately from this plan. No production collection in verification.

## Executed verification

- Isolated worktree and migrated disposable PostgreSQL `nova_request_learning_auto_test`.
- Task -> real database -> admin HTTP -> local HTTP proxy -> review/export -> CLI import;
  promotion correctly refused without full human review. Eight integration journeys pass.
- Offline runner: 597 passed, two existing expected failures. Report and JUnit at
  `.request-following-report/`; no live provider calls.
- Negative report controls: a deliberate JUnit failure with exit code zero, and missing
  JUnit, both produce failed status.
- Browser: synthetic session, source selection, intent approval and packet download;
  desktop and 390px phone layout inspected; no console errors. Local screenshots in
  `.dev/request-following/review-desktop.png` and `review-mobile.png` (not committed).
- Production collection, grants, prompts and provider configuration were not changed.

- Ownership negative control: a corrupt cross-account active-job link initially leaked
  a synthetic marker via raw integrity diagnostics. The collector now removes video
  metadata for detached jobs; the real-database journey passes and still flags the
  broken link. The existing operator diagnostics endpoint is unchanged.

All-users scope extension (explicit user clarification): before implementation,
register that enabling all-users mode must include other eligible accounts, exclude
accounts without recorded eligibility, retain immutable holdout assignment, and
fail closed on malformed configured IDs. Collection remains disabled by default.

- Explicit all-users mode: eight database journeys pass. Default remains off;
  all-users still requires recorded eligibility and rejects malformed scope IDs.
- CLI capture ownership negative control also failed before the fix and now passes:
  detached video metadata and unowned job debug bodies are withheld from captures.
- GitHub daily-regression workflow passed on the initial follow-on draft head.

Private-store review extension (requested before implementation): the real imported
captures must be visible without production writes. Preregistered failures: an
unreceipted, wrong-owner, expired, held-out or changed source must not appear; a
review must retain creator-source pointers and optimistic version checks, and must
never become full fixture approval; export must preserve eligibility provenance;
local HTTP origin/session protections must apply in file mode as well. Private
copies must be labelled with their eligibility check time, not current live consent.

- Private-store CLI import -> real local HTTP review -> export journey passes, including
  missing/wrong-owner/expired receipts, source tampering, holdout exclusion, origin
  checks and stale-review rejection. Full offline report: 597 passed, two expected failures.
