# KRI-228: Jev brief-verification feasibility pilot

Status: IMPLEMENTED — PRODUCTION ROLLOUT NO-GO
Planned at: `1c56e4449`
Issue: KRI-228

## Outcome

Decide whether TypeSafe Jev can accurately and cheaply shadow-check a typed Creative Brief
against Nova's proposed edit text and existing media descriptions. The pilot must not alter a
creator-visible reply, draft, render, or regeneration decision.

This is an additive quality check, not a cost-saving replacement: Nova's current Creative Brief
receipts in `app/kria/brief_checks.py` are deterministic and make no provider call. A successful
pilot may justify replacing the later standalone `ConformanceFeedbackAgent` call, but that is a
separate rollout.

## Scope

1. Add a small HTTP adapter for the pinned `jev-1.13.0` model. Use the documented
   `POST /v1/systemone` API, Noul questions, bounded timeouts, at most one safe retry, and typed
   failures. Keep numeric, timing, count, and ordering checks in code.
2. Project only relevant text into a bounded shadow payload:
   semantic brief requirements; proposed titles, story structure, and shot labels; plan shape;
   and existing media labels. Ask one question per required element and one question per
   candidate claim. Never send raw media or storage paths.
3. Dispatch the shadow check asynchronously after a Kria draft commit. A default-off flag and
   missing-key guard must make the path inert. Queue, provider, parse, persistence, or timeout
   failures are swallowed and recorded; existing receipts and replies remain authoritative.
4. Persist bounded run metadata in `agent_run`: owner IDs, model, attempts, latency, token usage,
   estimated cost, probabilities, and error code. Do not persist raw creator text or vendor
   response bodies.
5. Add a reproducible evaluation harness and labeled-case manifest. Reuse the existing
   request-following corpus where its hand-authored requirements/reference edits apply, keep
   derived negative cases marked separately, group splits by footage lineage, and freeze a
   held-out subset.
6. Produce a feasibility report with quality, coverage, p50/p95 latency, billed tokens, cost,
   retry/fallback rates, pipeline-wide impact, failure cases, thresholds, and an explicit go/no-go.
   Missing provider credentials or human adjudication are reported as blockers, never filled with
   invented measurements.

## Architecture and data flow

```text
Kria planner -> deterministic draft + Creative Brief receipts -> commit (authoritative)
                                                               |
                                                               +-> enqueue Jev shadow task
                                                                    |
                 flag/key off ------------------------------------> skip
                                                                    |
                 bounded text projection -> Jev HTTP adapter -> raw probabilities
                                                                    |
                 any failure -------------------------------------> AgentRun failure only
                                                                    |
                 success -----------------------------------------> AgentRun shadow metrics only

No Jev output reaches: reply text, requirement_receipts, draft state, approval state,
render dispatch, regeneration, or the frontend.
```

Component boundaries:

- `app/services/jev_client.py`: provider wire contract only.
- `app/services/jev_brief_shadow.py`: pure projection, question construction, and result mapping.
- `app/tasks/jev_shadow.py`: config gate, best-effort provider call, bounded persistence.
- `app/tasks/kria_runtime.py`: enqueue only after the authoritative transaction commits.
- `tests/evals/jev/`: fixture schema, grouped split, scoring, metrics, and report command.

## Failure and security rules

- Never retry read/write timeouts because the server may already have evaluated the request.
  Retry only connect failures and documented 429/529 responses, once, with bounded backoff.
- A 401/422, malformed answer, missing question, wrong answer type, or model mismatch is terminal
  for that shadow run and cannot affect the primary flow.
- The API key exists only in settings/secret storage and the Authorization header. It never enters
  logs, fixtures, command arguments, `agent_run`, or pipeline traces.
- Creator text is sent to TypeSafe only when the explicit default-off shadow flag and key are both
  present. AgentRun input stores hashes/counts, not the text.
- The provider's published terms do not promise zero data retention by default. Production content
  remains out of scope until the owner explicitly approves that data-handling posture.
- Input lists and text lengths are capped before enqueue and before HTTP serialization.

## Evaluation contract

- 100-200 cases or decisions, grouped by creator/footage lineage rather than random rows.
- Required strata: good, deliberately flawed, ambiguous, and relevant non-English cases.
- Labels distinguish `human_authored`, `human_adjudicated`, and `derived`; only human-adjudicated
  held-out results may authorize rollout.
- Tune candidate thresholds on calibration data only, then evaluate the frozen held-out split once.
- Required-element metrics: precision, recall, miss rate, abstention/coverage, macro/micro results.
- Unsupported-claim metrics: false-flag rate, missed-violation rate, precision/recall, severity split.
- Operations: p50/p95 latency, input/output tokens, cost/case and total, retries, errors, fallbacks.
- Baseline statement: deterministic receipts cost $0 and remain authoritative. Jev's full shadow
  cost is pipeline overhead. Replacement economics are estimated separately for
  `ConformanceFeedbackAgent`, including the still-required clip-metadata call.

Provisional rollout gate, to be finalized before held-out evaluation:

- required-element recall >= 95% at >= 80% coverage;
- unsupported-claim false-flag rate <= 5% and miss rate <= 10%;
- p95 provider latency <= 1.5 seconds;
- fallback/error rate <= 2%;
- no material English/Turkish regression;
- measured savings for a genuine replacement must exceed the engineering and fallback overhead.

## Test coverage map

```text
CODE PATHS                                      COVERAGE REQUIRED
JevClient.evaluate
  |-- valid Noul response                       unit: parsed model/answers/usage
  |-- 429/529 then success                      unit: one bounded retry
  |-- connect failure then success              unit: one safe retry
  |-- timeout / 401 / 422 / malformed JSON      unit: terminal typed errors, no secret leak
  `-- missing/wrong answer or model              unit: fail closed for shadow run

build_shadow_payload
  |-- semantic requirements                     unit: relevant items only
  |-- exact timing/count/order requirements      unit: excluded from Jev
  |-- proposed titles/story/labels               unit: bounded, stable candidate IDs
  |-- media descriptions                         unit: no paths/URLs, bounded lengths
  `-- empty relevant state                       unit: skip without provider call

run_jev_shadow task
  |-- flag/key off                               task unit: no HTTP/no mutation
  |-- success                                    task unit: sanitized AgentRun metrics
  |-- queue/provider/parser/persistence failure  task unit: primary flow unchanged
  `-- enqueue after commit                       Kria task regression test

evaluation harness
  |-- fixture/schema/provenance validation       replay unit
  |-- grouped calibration/held-out split         replay unit: no lineage leakage
  |-- threshold/confusion matrices               scorer unit including abstentions
  |-- latency/cost/fallback aggregation          report golden test
  `-- live provider run                          paid eval, blocked without credential

USER FLOW
creator asks for edit -> draft/reply unchanged -> optional background shadow run
  |-- Jev healthy                                no creator-visible difference
  `-- Jev down/misconfigured                     no creator-visible difference
```

## Implementation ledger

- [x] Provider adapter and unit tests
- [x] Bounded brief projection and unit tests
- [x] Default-off asynchronous shadow task and post-commit enqueue
- [x] Settings, `.env.example`, worker registration, and fail-open regression tests
- [x] Reproducible eval schema/scorer/report and 100-decision manifest
- [x] Live calibration/held-out run, or an explicit evidence-backed blocker
- [x] Feasibility report and go/no-go
- [x] Scoped backend tests, Ruff, and pre-ship checks (215 tests; preship green)

## GSTACK REVIEW REPORT

### Architecture

The typed Creative Brief seam is the correct semantic target, but it is not an existing AI cost.
The design therefore keeps Jev additive, asynchronous, and non-authoritative. The later
`ConformanceFeedbackAgent` is the first plausible replacement target if the pilot passes.

### Code quality

Provider transport, domain projection, orchestration, and evaluation stay separate. The shared
Gemini `ModelDispatcher` is intentionally unchanged because Jev's probability API is not a
generative model protocol.

### Tests

Every provider, projection, orchestration, and report branch above needs a focused test. Live
quality claims require a paid provider run and human-adjudicated held-out labels; replay tests alone
cannot satisfy that gate.

### Performance

The provider call runs off the creator request path. Payload and question counts are capped, retry
is bounded to one safe attempt, and persisted telemetry is sufficient to compute p50/p95 latency,
fallback rate, token usage, and cost without storing creator text.

### Review disposition

Proceed with the default-off shadow infrastructure and offline harness. The independent review
found and corrected a disabled-path receipt regression plus four initial manifest-contract defects
(payload shape, IDs, lineage, and ineffective mutations). Default to **NO-GO for production
rollout** until a credentialed run on human-adjudicated held-out data clears the documented gates
and TypeSafe's production data-retention posture is approved. Full evidence is in
`docs/reviews/kri-228/feasibility.md`.
