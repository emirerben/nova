# Kria agent runtime v2

Kria runtime v2 turns the existing creation thread, Main Creator Agent, editor,
and render pipeline into one durable creator workflow. It is a domain controller
for Nova editing, not a general assistant and not a second renderer.

## Golden path

```text
user message
  -> append semantic thread event + durable turn
  -> async planner receives a trusted bounded snapshot
  -> deterministic policy validates tools, risk, evidence, and revisions
  -> read/reversible strategy or editor draft OR exact approval
  -> typed receipt
  -> observed assistant response
  -> existing editor commit or Job dispatcher/render/review when approved
```

The model may propose intent. Server code owns authorization, revisions, risk,
idempotency, storage resolution, execution, and completion language. A plan is
never evidence that work happened. Only a settled receipt can support “changed,”
“rendered,” or other completed-action copy.

## State ownership

| Concern | Authority |
|---|---|
| Creator-visible transcript | append-only `CreationThreadEvent` |
| Runtime selection | immutable `CreationThread.runtime_version` |
| One user message lifecycle | `CreatorAgentTurn` |
| Reversible working edit | immutable, revisioned `CreatorEditDraft` |
| Consequential consent | `CreatorAgentApproval` |
| Tool result and side-effect identity | `CreatorAgentExecution` |
| Rendered media | existing `PlanItem` and `Job` |

Runtime-v1 threads keep their existing controller and projections. A thread never
changes runtime owner because a flag changes during its lifetime.

New threads also remain runtime v1 when the field is omitted. During internal
testing only, a client may request `runtime_version: 2` with no inline message
after `KRIA_RUNTIME_V2_ENABLED=true`; its first creator message then goes through
the durable `/turns` endpoint. This explicit opt-in prevents an old web build
from silently entering the new controller during a staggered deploy. Legacy
`/messages` and `/actions` mutations reject v2 threads, so they cannot bypass
the new turn ledger or render-consent boundary.

## Tool contract

`app.kria.registry.KRIA_TOOLS` is the only runtime-v2 tool registry. Each entry
owns its argument/result schema, version, capability, risk, execution mode,
retry policy, failure class, and executor binding. The model manifest and checked
contract snapshot are generated from it.

To add a read-only tool:

1. Define strict Pydantic argument and result models.
2. Add one deterministic executor and one registry entry.
3. Add a whole-turn fixture and expected receipt/semantic response.
4. Run `make verify-kria`.

Reversible draft tools must additionally validate the authoritative draft and
write a new revision atomically. Approval-required tools must never execute from
the planner output; deterministic policy creates a pinned approval first.

After a playable generation exists, the planner uses the existing versioned
Edit Copilot parser against a server-derived snapshot. The snapshot contains
only bounded text, caption, timeline, capability, and opaque target metadata;
storage paths and signed URLs are excluded. `draft.apply_editor_ops` compiles
the portable launch subset into Nova's full-section `EditorCommitRequest`:

- timeline reorder, remove, trim, split, duration, transition, and look;
- text/title content, timing, and parity-verified style fields;
- caption text, replacement, timing, emphasis, and presentation settings;
- music removal and mix changes;
- add unused sources, image duration, and image stacking;
- separately rendered, revision-fenced reviewed speech-cut candidates.

The compiler is non-mutating. Approval consumption calls the existing editor
commit or speech-cut validator while the exact Job is locked, records the new
generation in the accepted execution, commits, and only then publishes. A lost
broker acknowledgement becomes `outcome_unknown`; it is never blindly retried.

## Conversation contract

Every assistant turn has one value class: decision, question, action, progress,
review, or recovery. Repeating the brief is not a value class. Internal planning,
tool selection, retries, and worker callbacks are projected into quiet status or
one generation lifecycle artifact, not conversational bubbles.

Failures use `KriaProblem`: stable code, phase, useful message, retryability,
recovery action, trace ID, current revision, and safe target identities. The
creator sees the problem, what stayed safe, and one next action. Operators can
follow `thread -> turn -> approval/execution -> Job/variant/generation/task`.

Runtime-v2 HTTP reads are bounded. `GET /creation-threads/{id}` returns the
newest page in chronological display order; `after_sequence=N`
returns later events and `before_sequence=N` pages backward. The `/delta` path
remains a compatibility alias, while internal editor refreshes request
`projection=full`. Pending approval cards fetch
`GET /creation-threads/{id}/approvals/{approval_id}` to obtain the current
server fingerprint, revisions, expiry, and consequence copy before deciding.
Framework validation, authentication, rate-limit, and unexpected failures on
these routes use the same `KriaProblem` envelope as runtime failures.

## Render consent

Every initial render and rerender requires a separate, expiring approval pinned
to the current draft, manifest, ownership epoch, Job/variant/generation, and
expected revisions. “Make this” may prepare the approval but cannot consume it.
Approval consumption and exact editor/Job commit happen atomically; broker
publication follows the commit through the existing deterministic Job dispatcher
or editor rerender task. Accepted editor executions persist enough dispatch
metadata for the reconciler to publish the same generation after process death.

## Local proof

From a prepared worktree:

```bash
make kria-replay FIXTURE=nermin-matcha-update
make kria-replay FIXTURE=render-approval-required
make kria-replay FIXTURE=ambiguous-render-dispatch
make kria-replay FIXTURE=stale-project-replan
make verify-kria
```

The replay uses no model, storage, broker, or renderer. It proves snapshot,
planner contract, policy, typed tool execution, receipt, and observed semantic
response across happy, approval, ambiguous-dispatch, and stale-state paths. Real
model/media/render validation remains a separate rollout gate. `make verify-kria`
also checks the generated registry/API types, the 240-case structural all-format
corpus, editor reducer, runtime state machine, HTTP envelopes, admin trace, and
migrations. It writes duration/flakiness metadata to
`test-results/kria-verify.json`; feature flags stay off until the live gates pass.
