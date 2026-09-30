# KRI-228 Jev feasibility report

Date: 2026-09-30
Decision: **NO-GO for production rollout; merge the default-off pilot infrastructure only.**

## Executive finding

Jev is technically suitable for bounded, text-only semantic decisions, but Nova has not yet
measured it on human-adjudicated Kria examples. The current Creative Brief receipt path is
deterministic code and makes no provider call, so adding Jev there increases cost and operational
surface rather than saving money. The first credible replacement candidate is the standalone
`ConformanceFeedbackAgent`; its prerequisite clip-metadata analysis would still remain.

The implementation therefore runs only as an optional post-commit shadow task. It cannot change a
reply, receipt, draft, approval, render, retry, or regeneration decision. The flag defaults off,
and a missing key makes the path inert.

## What was inspected

| Decision seam | Current behavior | Can Jev remove a call? | Finding |
| --- | --- | --- | --- |
| Creative Brief receipts | Local typed checks in `app/kria/brief_checks.py` | No | Baseline provider cost is $0; Jev is additive quality telemetry. |
| Conformance feedback | `gemini-2.5-flash`, text digest, up to two verdict attempts | Possibly | Best next replacement candidate, but clip upload and `ClipMetadataAgent` remain required. |
| Exact duration, counts, ordering, timestamps, asset validity | Deterministic code | No | Kept out of Jev by design. |
| Creative planning, script generation, transcription, vision, rendering | Generative/media pipelines | No | Outside Jev's constrained decision role. |

The conformance agent's checked-in price card is $0.000075 per 1,000 input tokens and $0.0003 per
1,000 output tokens. Jev's published price checked on 2026-09-30 is $0.042 per million input tokens
($0.000042 per 1,000) with output free. That unit-price comparison is not a measured Nova saving:
actual token counts, fallback frequency, and the still-required clip analysis determine the
pipeline-wide result.

## Provider contract and data posture

- Pinned model: `jev-1.13.0`; the pilot rejects any other configured model.
- Endpoint: `POST /v1/systemone`; questions use typed Noul answers.
- Retry policy: one retry only for connect failures or HTTP 429/529. Timeouts, 401, 422,
  malformed payloads, model mismatch, and answer-shape errors are terminal for that shadow run.
- Input is capped at 16 semantic requirements, 16 candidate claims, 40 media labels, 24 KB, and
  text only. URLs and storage-path-like media labels are discarded.
- Persisted input contains hashes and counts, not creator text, the API key, or raw provider bodies.
- TypeSafe states that customer data is not used for training, but the reviewed public material
  does not establish zero data retention for this account. Production creator content remains
  blocked pending an owner-approved data-handling review (or an applicable zero-retention
  agreement).

References: [models and pricing](https://docs.typesafe.ai/models),
[API reference](https://docs.typesafe.ai/api), and
[jev-1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

## Reproducible evaluation status

The committed builder produces 25 cases / 100 individual decisions from the existing
request-following fixture families:

- calibration: `east_run`, `food_day`, `harbor_run`;
- held out: `sport`, `trip`, `vlog`;
- strata: faithful good cases, deliberate omissions/unsupported claims, ambiguity, and Turkish;
- every row validates against the production `JevPayload` and question contract;
- fixture lineage never crosses calibration and held-out splits;
- labels are explicitly `derived` and `human_adjudicated=false`.

No `TYPESAFE_API_KEY` was available, and no human adjudication was performed. Consequently there
are no honest measurements yet for incorrect flags, missed violations, coverage, latency, billed
tokens, cost, retry rate, fallback rate, or English/Turkish quality deltas. The report command
forces **NO-GO** when predictions, held-out adjudication, a 100-200-decision set, or explicit
numeric gates are missing. Provider failures count as abstentions and reduce coverage; the harness
does not invent probabilities.

Commands:

```bash
cd src/apps/api
python -m tests.evals.jev.build_manifest
TYPESAFE_API_KEY=... python -m tests.evals.jev.run_live \
  tests/evals/jev/pilot_manifest.jsonl --out predictions.jsonl
python -m tests.evals.jev.report tests/evals/jev/pilot_manifest.jsonl predictions.jsonl \
  --required-threshold 0.5 --unsupported-threshold 0.5 \
  --threshold-source calibration \
  --gates tests/evals/jev/pilot_gates.json --out jev-report.md
```

## Rollout gates

Thresholds must be selected on calibration data, frozen, and then applied once to the held-out
split. Production rollout remains blocked unless all of these pass:

- required-element recall >= 95% at coverage >= 80%;
- unsupported-claim false-flag rate <= 5%;
- unsupported-claim missed-violation rate <= 10% at coverage >= 80%;
- p95 provider latency <= 1,500 ms;
- provider error/fallback rate <= 2%;
- no material English/Turkish regression on human-adjudicated cases;
- TypeSafe's production data-handling posture is explicitly approved;
- a replacement experiment shows net pipeline savings after retained clip analysis and fallbacks.

## Failure cases and rollback

Known model limits make Jev inappropriate for numeric arithmetic, exact counts/dates, open-ended
generation, or questions that combine several decisions. The adapter instead asks one atomic
question per requirement or proposed claim and keeps exact checks in code.

Rollback is immediate: leave `JEV_BRIEF_SHADOW_ENABLED=false` (the default) or set it false and
restart workers. Existing deterministic receipts remain the authority before, during, and after
the pilot.

## Next decision

1. Human-review the 100 seeded decisions, preserving the frozen footage-group split.
2. Approve provider data handling and supply a scoped test credential.
3. Run the pinned model, tune thresholds on calibration only, and publish the held-out report.
4. If the gates pass, shadow `ConformanceFeedbackAgent` with the same text digest and compare the
   verdict-only savings; do not remove `ClipMetadataAgent`.

Until those steps are complete, production rollout is not justified.
