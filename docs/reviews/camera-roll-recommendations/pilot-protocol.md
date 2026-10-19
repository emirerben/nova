# KRI-732 pilot protocol — draft, not preregistered or run

19 October 2026. No participant outreach, exposure or paid run is authorized by
this document. The implementation/report command performs no network calls.

## Freeze before exposure

Register the final protocol, its timestamp/hash, app/server revisions, active
capability cohort and private reliability/native-output evidence. Freeze latency
and settled-spend caps from a measured end-to-end baseline, not the metadata-only
benchmark. The report CLI requires those values; they have no silent defaults.
Preserve the original protocol and do not change bars after viewing results.

Proposed sample: 10 consenting creators, at least two distinct outings each.
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
expand/hold decision. Current decision: **hold — no pilot or story export has run**.
