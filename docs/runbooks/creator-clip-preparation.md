# Creator clip preparation (KRI-151)

KRI-151 adds an upfront semantic preparation step before the first footage-based
creator plan. When enabled, the API records the creator's request, analyzes the
source clips, stores generation-pinned semantic analysis, and only then resumes
planning. Every footage-planning turn creates a preparation attempt and uses
this background path, including turns where all clips are already ready. Those
turns read background generation metadata and perform any residual semantic
vision checks there; metadata checks do not trigger paid reanalysis, while
residual semantic questions may still be paid.
The preparation task does not approve or render an edit by itself.

The feature is controlled by the backend flag:

```bash
CREATOR_CLIP_PREPARATION_ENABLED=false
```

The default is `false`. There is no production toggle in this runbook. Enabling
the flag requires the rollout evidence below and an explicit operator decision.

## Durable execution model

Every footage-planning request creates one `CreatorPlanningAttempt` for the
source creator event. The database commit
happens before the Celery task is published, and the original planning inputs
are stored on that receipt. The attempt is the durable outbox receipt; it is not
a render authorization.

The task claims an attempt with an 1830-second lease, longer than the 1800-second
hard task limit, and processes at most three clips in parallel. It checkpoints
each completed analysis before continuing. A periodic reconciliation task
republishes queued work and expired leases, so a broker publish failure or
worker loss does not leave a committed preparation silently stranded. Delivery
is at least once; the attempt status, lease token, source digest, and generation
checks make re-entry safe.

The task resumes the original planning inputs only after all required analysis
is available. The resume path revalidates the attempt and its owning plan,
session, item, and sources before publishing any planning result.

## Source and concurrency guards

Preparation captures the source manifest and a digest of its media identities.
For each source, the worker reads the current object generation and refuses to
write an analysis if the registered generation changed. A cached semantic record
is reused only when its exact generation and analysis freshness are valid; the
worker still reads those identities on an all-ready turn. Probe-only, stub,
empty, or stale records are preparation misses and are reanalyzed in the
background. Generation metadata checks do not consume paid analysis budget;
residual semantic questions may still do so.

Every claim and checkpoint verifies all of the following:

- creator, session, plan, and item ownership still agree;
- the plan ownership epoch and creator-session revision match the attempt;
- the source digest still matches the current source manifest;
- the attempt lease token is still current; and
- the source object generation is unchanged.

If any fence fails, the attempt is superseded or stopped. A late provider result
must not overwrite a newer upload, session, plan, or preparation attempt.

Background clip membership and caption work runs with at most four concurrent
analyses, checkpoints completed batches, and is bounded by
`min(intents, 6) * 50` units of work. Foreground-only intent resolution retains
the cap `min(4, config)`. A terminal provider quota result or an unknown
provider outcome halts later background batches for that turn; provider details
remain private.

## Public progress and error contract

The creator session projection may include an optional `preparation` object with
`status`, `completed`, `total`, `message`, `error_code`, and `retryable`. Older
web and iOS clients omit this object safely; clients that understand it should
continue polling while the status is `queued`, `analyzing`, or `resolving`.

The public provider and planning outcomes are distinct:

| Outcome | Meaning | Recovery behavior |
| --- | --- | --- |
| `provider_quota_exceeded` | Gemini explicitly denied the call because of a quota, billing, or monthly spend limit. | The attempt fails with a safe retryable error. Do not treat a generic rate-limit 429 as this outcome. Retry after the provider limit is addressed. |
| `ai_budget_exhausted` | Nova's own AI cost-control reservation rejected the call before provider work. | The attempt fails with the cost-control error and remains retryable after the allowed budget or attribution is corrected. |
| genuine no match | Analysis completed, but the semantic request has no matching source clip. | Continue the creator conversation and ask a bounded clarification question. This is a planning result, not a provider outage. |

`provider_outcome_unknown`, `media_unavailable`, and `analysis_unavailable`
remain separate operational failures. They must not be presented as a genuine
no-match result. Safe user messages may describe the next action, but must not
include provider payloads, private source text, storage paths, or credentials.

## Rollout sequence

1. Apply the additive migration and verify the API is at the expected head.
   Confirm the `creator_planning_attempts` table and the session preparation
   projection column are present before enabling claims.
2. Deploy the API and workers with `CREATOR_CLIP_PREPARATION_ENABLED=false`.
   Confirm task registration, reconciliation scheduling, disabled behavior, and
   the unchanged legacy planning path.
3. Run an isolated staging canary with the flag enabled. There is no cohort
   allowlist for this server-side flag. Verify durable attempt creation, lease
   recovery, publish recovery, source-generation fencing, progress polling, and
   each public error category before considering any production enablement.
4. Verify the web client against both enabled and disabled responses. The web
   progress surface may show preparation status, but must remain compatible with
   older servers that omit `preparation`.
5. Verify the iOS client against both enabled and disabled responses. Its
   optional preparation fields must decode missing values as an inactive
   preparation state, and progress polling must not require a new render or job
   payload.
6. Consider production enablement only after the evidence ledger is complete.
   No frontend or iOS production toggle is required for this feature; those
   clients consume an additive progress projection.

Do not enable the flag merely because the migration or unit tests pass. The
acceptance evidence must use real source media and the deployed backend/worker
path.

## Rollback

Set `CREATOR_CLIP_PREPARATION_ENABLED=false` and restart the API and workers.
This stops new preparation admissions. Existing committed attempts are allowed
to drain through their leases and reconciliation path; the API and workers
release existing queued and running attempts through that path. Do not delete
queued or running attempts during rollback. A completed preparation may still
be read by the owning session, while new planning requests use the legacy path
once the flag is disabled.

Keep the additive migration and attempt rows in place. Do not downgrade the
migration or reuse an old source generation to force a result through a stale
guard. If queued work must be stopped for an incident, first record the attempt
ids and let the normal ownership and lease fences prevent late writes.

## Acceptance evidence

The following evidence is required before broad rollout and is intentionally
pending until it is produced from the deployed implementation:

- a deployed-worker run with 19 native clips, showing durable progress from
  queued through analysis and planning completion;
- a request with a different semantic instruction over the same 19 clips,
  showing that the semantic preparation is reused safely where generation and
  freshness match and that the planner resolves the changed instruction;
- a worker-loss or publish-failure recovery run proving the committed attempt is
  reclaimed without duplicate planning side effects;
- a source replacement run proving the old generation is rejected and cannot
  overwrite the newer source analysis;
- representative provider quota, AI budget, and genuine no-match responses,
  with their public categories preserved; and
- web and iOS polling checks against both the new projection and an older
  response that omits preparation.

Until these checks are recorded, KRI-151 should be described as implemented
behind a disabled flag with rollout evidence pending.

## Latest verification status

The latest release-candidate verification passed 768 backend tests, 13
PostgreSQL checks, 176 web tests, TypeScript checking, and the pre-ship gate.
The focused iOS verification now passes 47 tests after project regeneration.

An approved live-provider check analyzed all 19 native clips in a dedicated
local database and persisted progress through completion. The saved original
request resolved six clip intents and reached confirmation; a second request
resolved indoor grouping, a creator-authored caption, and outdoor ordering
without repeating clip analysis. Total provider cost was $0.18517 under the
shared $2 cap. This exercise exposed an async conversation-loading failure in
the preparation ownership check; the fix is covered by a real PostgreSQL
regression test.

This check invoked the task body locally, without Celery delivery, production
writes, confirmation, or rendering. It verifies preparation and planning with
real footage; it does not replace deployed-worker, device polling, or rendered
output acceptance and does not authorize a production toggle.
