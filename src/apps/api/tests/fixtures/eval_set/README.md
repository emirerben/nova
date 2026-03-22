# Eval Set — Scoring Quality Fixtures

This directory holds ≥20 human-rated raw video fixtures used by the launch-gate
scoring quality test (`tests/quality/eval_scoring.py`).

## Format

Each fixture is a subdirectory:

```
eval_set/
  fixture_001/
    raw.mp4             ← source video (≤10 min, 16:9 or 9:16)
    ground_truth.json   ← human rating
  fixture_002/
    ...
```

`ground_truth.json` schema:
```json
{
  "best_start_s": 45.0,
  "best_end_s": 95.0,
  "notes": "Strong emotional hook at 0:45, high energy throughout"
}
```

## Launch gate

`pytest tests/quality/ -v` must pass before launch.
Target: recall@3 ≥ 70% (Nova's top 3 contains the human-chosen clip in ≥70% of cases).

## Adding fixtures

1. Add `raw.mp4` + `ground_truth.json` to a new subdirectory
2. Run `pytest tests/quality/ -v` to see impact on recall score
3. Video files are git-ignored — store in GCS: `gs://nova-videos-dev/eval_set/`
