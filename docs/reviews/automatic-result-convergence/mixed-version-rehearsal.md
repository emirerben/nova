# Mixed-version rehearsal: proposal, not executed

Frozen application source: `973391cace1f01beddfeb3763e395c41ec4e448f`, tree `64252985c7b396cb54e5a864116fa84ccb6127f4`. Compare prior captured-input executor `72c248d5ccd8600f93f8f6fa6abe1666dba688a8` and known claim-before-provider predecessor `62ac28e0909f69fb2531dc4101a7be6081ef7cee`. Main `c577a5fa5004f8edb84e607e7f6ff9a59920ed23` is an incompatible pre-feature API/worker baseline, not a supported consumer of new delivery tasks. These are source identities, not verified deployed images. Record image digests when building the disposable rehearsal; none are known here.

## Observable acceptance and finite matrix

Every row uses an isolated disposable PostgreSQL database, private broker/vhost, synthetic tenant and counting fake provider. GET must never increase the provider count or enqueue work. Preserve the original UUID, original envelope, tenant, source authority and claim token; record persisted state before and after each process boundary. At most one paid dispatch per original claim. Do not infer nonexecution from absent fields.

| API / consumer | Request boundary | Expected result and limit |
| --- | --- | --- |
| New / new, identical source and manifest | Commit before first enqueue, duplicate delivery, concurrent claim | Original request eventually completes once. Same UUID and payload, one committed claim before provider. Delayed reads converge automatically. |
| Captured-input old / matching old | Intact pending envelope, zero claim evidence | Matching executor can finish the original authorized request once before transition. No reconstruction from current draft. |
| Captured-input old / new | Intact pending envelope, changed executor digest | Safe terminal rejection only while the conditional claim predicates still prove never-claimed. Provider count stays zero. This is safe refusal, not automatic fulfillment. Do not advertise zero-friction completion for this transition. |
| New / captured-input old | New manifest handed to an old consumer | Unsupported combination: old consumer may reject an intact new request before execution. Isolate queues/admissions; do not permit this during rollout. Additive columns do not establish semantic compatibility. |
| Pre-feature main API or worker / new rows or task names | Missing receipt capability or unknown consumer | New client refuses paid admission without capability. Old client/API retry behavior is not certified. Do not send new task names to old workers; require explicit drain/isolation. |
| New API / original older claimed worker | Crash after claim before dispatch, or after dispatch before result persistence | Running eventually projects unknown; redelivery never claims again, even if count is zero. A claim without receipt cannot prove provider nonexecution. No lease expiry, claim reset or paid resend. |
| New API / original older claimed worker | Exact original token publishes late | Pending/running/unknown can complete once with the existing nonnull token. Wrong/null/duplicate token cannot publish. Subsequent GET returns the existing result. Current-code digest does not invalidate an already-claimed completion. |
| New API / ambiguous legacy row | Missing payload, timestamps or claim evidence; older process may be live | Durable unknown, observer-only. No definitive failure, cancellation certificate, reconstruction or new paid request. A legitimate older executor's exact token may later complete; tokenless outcome can remain unresolved. |
| Either compatible version / matching original | Stop before claim versus Stop racing committed claim | First winner decides: proven-unclaimed cancellation is terminal with zero provider calls; after claim, Stop cannot assert external execution stopped. Keep observation/intent fences, and never turn Resume into a new authorization. |
| New / new or original late worker | Source/draft/tenant changes, new instruction, account switch | Before claim, exact original authority mismatch rejects without provider. After claim, retain original result under its UUID; no automatic write over a new draft, account or source. Mismatched client binding becomes a reviewable proposal. |
| New / new | More than 50 ineligible legacy rows plus eligible new work, missed publication | SQL filters legacy rows before LIMIT; eligible original work progresses. Publication pacing remains durable and capped at 300 seconds between attempts; no hot loop. |

Rejection must be tied to the conditional never-claimed predicate at the database mutation, including a race where an old process wins the claim first. If that predicate fails, preserve unknown/running and accept the original late token. A missing field alone never justifies the terminal row above.

## Version transition that preserves legitimate work

There is no automatic version-routing queue or cross-version envelope translator in this patch. A mixed fleet must not consume shared incompatible envelopes. To preserve healthy unclaimed requests, an authorized release owner must pause new admissions and new-version sweeping consistently, retain the matching old executors long enough to drain compatible intact envelopes, and verify outcomes before replacing consumers. Claimed/unknown work remains observable under its original identity; never wait by resetting claims. If a matching executor cannot be retained, preserve uncertainty or accept an explicit safe pre-execution rejection only for proven-unclaimed work. Do not call that seamless completion. There is no automatic safe recovery for an unknown provider outcome without original result evidence.

Proposed order, requiring separate operational approval: inventory exact images/flags/schema/queues and in-flight receipts; rehearse additive schema 0132 through 0138 on synthetic populated data; pause admissions and compatible sweeps; drain or isolate version-specific consumers and preserve any original claimed executor capable of publishing; deploy one verified compatible API/worker/light/Beat image; verify schemas, manifests and consumer routes; resume only after matching-version fixture evidence passes. No flags, queues, migrations or deployed services were touched here. Future merge can automatically deploy and migrate, so merge is itself gated by this readiness plan.

## Smallest next resource request

After reset explicitly releases the lane, request one isolated backend slot: one PostgreSQL instance/database, one private broker and one fake-worker process at a time, no Xcode/simulator/Java/TLC. Begin with existing current-source tests below; collect JUnit and exact executed node IDs. Budget 15 minutes for the bounded initial batch, then stop and report failures rather than expanding to aggregate suites. The real two-version process/queue harness and new code-digest race controls are not implemented/executed yet; they remain a named release gate, not an existing passing test.

Proposed current-source command from `src/apps/api`, after the exact-database guard and no-competing-owner preflight:

```sh
/tmp/nova-ai-db-safe-python -m pytest -q \
  tests/routes/test_slide_post_ai_requests_postgres.py \
  tests/routes/test_slide_post_ai_legacy_pending_postgres.py \
  --junitxml=/tmp/nova-auto-mixed-version-current.xml --tb=short -rs
```

Those modules cover claim/authority/config races, lost enqueue, worker death, cancellation, legacy fairness and original-token late publication. Existing `test_pending_changed_authority_or_executor_rejects_without_paid_call` varies draft/source/config/project; despite its name it does not vary a real old/new code digest. Add explicit old-envelope/new-digest rejection and simultaneous old claim/new rejection controls before claiming the digest transition is proven. Use separately pinned subprocess entrypoints or immutable synthetic images, provider fake transport only, and assert the resulting database receipts through the real store. Record actual binaries and payload provenance; mutating a fixture digest alone is a negative control, not rolling-binary proof.

No such command was run during reset's slot. Current-source native/package/UI, model, Linux aggregate, migration rehearsal, broker/Beat and physical-output gates remain unexecuted. Broad tracking is unresolved; archived KRI-935 is provenance and KRI-1233 is reset-only.

## Hosted snapshot

For source 973391cac, CI run 42270005379 is now running: changes/select and journey-mapping passed; API shards 0 and 1 are in progress. Request-following run 42270005441 was pending at its last direct read. Docker run 42270005327 is in progress. Overlay, font, extension, cache-version and release guards passed. Replacement staging-plan, PR-evidence and local-test checks passed; their earlier attempts were cancelled. No exact-head failed check was reported at this read. This is a snapshot, not an aggregate or native success.
