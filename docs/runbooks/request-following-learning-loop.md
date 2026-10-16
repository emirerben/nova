# Request-following learning loop

KRI-215 connects private session captures to the existing incident corpus, gate
ledger and CI journeys. It does not change prompts, call a provider, spend money,
write production data or infer success from a reply or merged PR.

Run commands from the repository root. Use `src/apps/api/.venv/bin/python` for
promotion (it imports the existing incident schema); other commands use stdlib.
Set a private directory outside all Git checkouts, with permission 700:

```bash
mkdir -m 700 /private/tmp/nova-learning
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning \
  capture --prod --thread <explicit-thread-uuid>
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning \
  import --input <capture-path-from-previous-command> --split development
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning \
  propose --session <session-key-from-import> --id kri123-example --ticket KRI-123
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning \
  report --output /private/tmp/nova-learning/coverage.json
```

Capture invokes only `scripts/admin.py GET`, using existing events, turns,
trace and integrity endpoints, plus linked plan-item and job debug snapshots.
It paginates events/turns, preserves source
payloads and fails with exit 2 on incomplete evidence. Partial captures remain
inspectable; missing endpoints are explicit. No media is downloaded. Events may
contain requests, follow-ups, media changes and editor state; turns retain agent
plans, executions and receipts. These endpoint snapshots are not an atomic DB
snapshot: collect settled sessions, and inspect revision/time consistency.
Raw provider output may already be redacted upstream. A recorded recipe or a
successful tool receipt does not prove final pixels/audio or a durable save.

Import is repeatable: unchanged content has the same session/source identity;
changed content retains old snapshots and invalidates older proposals. Review
snapshots are retained in `reviews/`. Run one writer per private store. Back up
the store with your approved private evidence storage; `/private/tmp` is not a
durable archive. Delete private stores when no longer needed under normal media
retention policy. Do not commit them, raw videos, signed URLs or account exports.

## Review a proposal

Edit the private candidate JSON. Automatic extraction indexes creator messages,
follow-ups, media events, draft-applied events and turn plans. It does **not**
translate assistant statements into ground truth. Fill in:

- `expectations`: independently stated outcomes, each with JSON pointers to
  creator message content, e.g. `/sections/events/17/content`. Human reviewers
  must check that those messages actually justify the expected outcomes.
- `loss_stage`: understanding, handoff, operations, persistence, rendering, or
  unknown. This is a reviewed diagnosis, not an automatic classifier.
- `context`: available/missing evidence and source pointers. Do not infer manual
  editor history from a saved draft alone.
- `ambiguities`: unresolved creator choices; approval blocks until resolved.
- `variations`: author proposed paraphrases, combined requests, different media
  facts and multi-turn corrections from the supplied work prompts. Variants are
  not automatically equivalent and remain unreviewed until independently checked.
  Use a new candidate id with the same session/source for each promotable variant.
- `incident`: an ordinary `tests.incidents.models.IncidentRecord`, with real
  executable expectations and a resolvable existing test command. The command
  should name the corpus test file before a new id exists; after promotion use
  `-k <id>` to run the new case. Do not invent production strategies/receipts to
  fill unavailable fields. Unsupported scenarios need a focused real harness
  before promotion, rather than a schema-only fixture.
- `evidence`: understanding, persisted execution and rendered output stay
  independent. Each pass/fail needs `level`, a local `artifact`, its SHA-256,
  `command`, and `measured_at`. Artifact bytes must match on review and promotion.
  Hash validation establishes integrity, not truth: the reviewer must inspect
  what the artifact actually proves. Missing/unassessed evidence is allowed and
  must never be reported as a pass.

Allowed levels: understanding = offline replay or live provider; persistence =
integration DB or production capture; output = render artifact or native device.
Offline replay ignores the prompt in existing recorded-response runners. It can
pin adaptation/execution behavior but cannot establish prompt improvement or
provider request-following. A filename or self-authored result cannot establish
render proof. Use the existing [journey evidence requirements](../reviews/kri-559-journey-verification.md).

A human reviewer records their decision after inspecting the exact proposal:

```bash
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning \
  review --candidate <candidate-path> --decision approved \
  --reviewer '<human reviewer>' --rationale '<basis and limitations>' --redaction-checked
src/apps/api/.venv/bin/python scripts/request-following-loop.py \
  --store /private/tmp/nova-learning promote --candidate <candidate-path>
bash scripts/check-api.sh test tests/incidents/test_incident_corpus.py -k <case-id> -q
```

The tool records human attribution; it cannot authenticate the reviewer. Agents
must not approve on a human's behalf. Editing the source, expectations, incident,
variants, or evidence invalidates the review. Rejected cases cannot promote.
Promotion validates current schemas, refuses overwrite and copies only the
reviewed incident into the existing corpus with capture/reviewer provenance.
It reports the verification command but does not execute arbitrary candidate
commands. Standard PR review and CI still govern the new fixture. Link new
production guards to `tests/gates/reliability-gates.json` and the relevant journey
when the fixing PR has measured evidence; do not invent a gate for an unknown fix.

## Holdouts and subsequent improvements

Import unseen conversations with `--split holdout` before examining them for
prompt tuning. The conversation split is immutable within the store. The CLI
refuses candidate generation/promotion for holdouts and reports only their count.
Keep holdout content in a separate access-controlled evaluation workflow; this
CLI does not run or score unseen conversations. Do not move a conversation or
its paraphrases to development by creating another store. Previously inspected
or fixture-backed sessions (including Tigre) are development evidence.

For each proposed prompt change: review expectations first, run the existing free
regressions, then obtain the required budget approval for live/provider evals.
Compare baseline and proposed prompt on identical cases and explicit model/prompt
versions, with independent held-out conversations. Require persisted execution
and real output evidence for those claims. Record regressions and missing proof,
not just aggregate pass counts. No automatic production prompt mutation exists.

Repeat capture/import/report after real corrections and fixing PRs. The report
joins ticket references across all JSON fixtures, incident records, reliability
gates and journey ids, lists missing context, and flags stale review/source.
Matches are references only, not measured coverage; inspect the referenced tests.
Explicit session ids are required: account-wide discovery and collaborator
identity resolution are intentionally left to an operator. There is no scheduled
collector in this first slice.

## Verification

Failure cases were written before implementation in
[`kri-215/verification-plan.md`](../reviews/kri-215/verification-plan.md).
The CLI acceptance journey imports twice, rejects stale/holdout/unsupported
proof, paginates a partial capture, promotes a synthetic human-reviewed case
inside a temporary repository, and runs the real corpus contract assertion.

```bash
bash scripts/check-api.sh test tests/incidents/test_request_following_loop.py -q
```

CI journey `kri-215-learning-loop` selects that acceptance test when the CLI changes.
