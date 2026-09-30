# Jev offline evaluation

The harness is deterministic and offline. Build the pilot seed from the existing
request-following corpus with:

```bash
cd src/apps/api
python -m tests.evals.jev.build_manifest
```

`build_manifest.py` writes `pilot_manifest.jsonl` (25 cases / 100 decisions),
with good, flawed, ambiguous, and Turkish strata and disjoint footage lineage
groups between calibration and held-out. It intentionally writes no predictions.
The seed labels are not human-adjudicated, so any report containing this manifest
must be NO-GO until a person reviews the held-out labels.

After human review, run the pinned provider adapter. The key is read only from
the environment and is never accepted as a command-line argument:

```bash
TYPESAFE_API_KEY=... python -m tests.evals.jev.run_live \
  tests/evals/jev/pilot_manifest.jsonl --out predictions.jsonl
```

Offline reporting (after a human or test process supplies a separate prediction
JSONL; the builder never creates predictions):

```bash
python -m tests.evals.jev.report tests/evals/jev/pilot_manifest.jsonl predictions.jsonl \
  --required-threshold 0.5 --unsupported-threshold 0.5 \
  --threshold-source calibration \
  --gates tests/evals/jev/pilot_gates.json --out jev-report.md
```

Only `run_live` calls the provider. No command claims live results without a
separate predictions file. A report without explicit numeric gates or a declaration
that thresholds were frozen from calibration is NO-GO; the quality tables score
held-out cases only.
