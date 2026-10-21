# KRI-732 pilot protocol — draft, not preregistered or run

19 October 2026. The creator authorized a single-account pilot and subsequently allowed model calls
when needed for a real test, under the discussed $2 total ceiling. No other participants
or outreach are authorized. This document does not widen that scope. The implementation/report command performs no network calls.

## Freeze before exposure

Register the final protocol, its timestamp/hash, app/server revisions, active
capability cohort and private reliability/native-output evidence. Freeze latency
and settled-spend caps from a measured end-to-end baseline, not the metadata-only
benchmark. The report CLI requires those values; they have no silent defaults.
Preserve the original protocol and do not change bars after viewing results.

Current study: one creator account, two distinct outings where available. Use
`scope: "single_creator"`, `creator_count: 1`, and `total_budget_usd: 2`;
preregister a per-session limit within that total before exposure. Even a passing report is only `eligible_for_account_review`
and cannot support wider rollout. No grounded story is a valid unavailable outcome. Cache hits come first; new calls
require the explicitly configured shared run-budget reservation.

The original wider-rollout hypothesis was 10 consenting creators, two outings each.
It remains untested and is not authorized by the current study.
Hold out creators and events from retrieval/recommendation tuning. Keep internal
stress runs separate. Where feasible, compare with creator-selected footage and
mask candidate identity during independent export review.

Proposed bars: >=50% of eligible sessions select a recommendation; >=80% of
selected recommendations fulfill the story and required instructions in a
verified export within two corrective turns. Approval, selection, ready Job,
acknowledged export and independently reviewed fulfillment are separate events.

## Collect privately

Use pseudonymous session/creator/collection keys. Retain invitation, consent,
eligibility, exposure, no-selection, rejection/abandonment, corrections, approval
snapshot hash, actual export hash and snapshot binding, human fulfillment review,
latency, model calls and settled spend. Evidence links refer to the existing
private review storage; never commit source footage, transcripts or signed URLs.
Record required-instruction misses and unknowns, including failed/abandoned work.

Repeat permission revocation, unavailable/deleted originals, ownership and cache
checks on real phones. Record cohort rollback while confirming historical posts
and accepted outputs remain available. Run the requested-edit reliability gate
and inspect native outputs before considering expansion.

## Evaluate without changing the bar

From the API directory:

```bash
.venv/bin/python -m app.cli.camera_roll_pilot \
  --protocol /private/path/frozen-protocol.json \
  --sessions /private/path/pilot-sessions.json \
  --output /private/path/aggregate-report.json
```

The JSON contracts are `PilotProtocol` and `PilotSession` in
`app/cli/camera_roll_pilot.py`. Empty/missing evidence, insufficient sample,
late registration, resource-cap failures and unfulfilled output prevent promotion.
Internal stress sessions never inflate pilot denominators. The aggregate contains
no participant or collection keys. `eligible_for_review` requests a human go/no-go
decision; it never enables a flag or independently certifies private evidence.

The dated final report must link the frozen protocol and privately reviewed
outputs, report the denominators/unknowns/failures and record an explicit human
expand/hold decision. Current decision: **hold — no real-account recommendation pilot has run**. The two private own-upload proxy previews are internal examples, not pilot sessions; no fulfillment/conversion result is inferred from them. Internal
synthetic physical exports are recorded separately in [physical-export.md](physical-export.md).

## Frozen-label retrieval comparison

Prelabel source intervals independently before tuning. Freeze the exact labels file checksum
in `Protocol.labels_sha256`, then record text and semantic results for each query. Include
lit-house, ferry, long-take endings, late speech, occlusion and no-match controls.
`app/cli/camera_roll_retrieval_eval.py` compares top-ten accepted-interval recall and reports
unusable/irrelevant returns, no-match precision/recall, unknowns and resource caps. The
smallest passing method is recommended for human review; references are not self-verifying.

```bash
.venv/bin/python -m app.cli.camera_roll_retrieval_eval \
  --protocol /private/path/retrieval-protocol.json \
  --labels /private/path/frozen-human-labels.json \
  --runs /private/path/recorded-results.json \
  --output /private/path/aggregate-retrieval-report.json
```

This offline report makes no calls, creates no labels and changes no rollout settings.
The account study prefers existing eligible cached results. New semantic/inspection
results require the approved shared run budget; budget refusals remain unavailable. Human labels and private source evidence have not been supplied.
