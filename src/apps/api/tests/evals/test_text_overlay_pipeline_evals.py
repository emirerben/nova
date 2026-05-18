"""End-to-end eval gate for the Layer-2 text-overlay pipeline
(``app.pipeline.text_overlay_v2.pipeline.run_full_pipeline``).

Unlike the Layer-1 agent evals (``test_template_text_evals.py``), this file
evaluates the *multi-stage pipeline* (OCR + grouping + alignment + classification)
rather than a single Gemini agent call.  The pipeline is behind the
``text_overlay_v2_enabled`` feature flag in prod; evals call it directly and are
independent of flag state.

Design doc: ``~/.claude/plans/template-text-overlay-layer-2-architecture.md``
Evaluated by: PR #214 (slice G — orchestrator wiring + flag-gated routing).

Run modes (see ``tests/evals/README.md`` for full guide):
  pytest tests/evals/test_text_overlay_pipeline_evals.py -v
  pytest tests/evals/test_text_overlay_pipeline_evals.py -v --with-judge
  pytest tests/evals/test_text_overlay_pipeline_evals.py -v --eval-mode=live --allow-cost

All tests skip today (no ``.transcript.json`` sidecars exist yet).  The eval gate
is dormant until the sidecars land — same dormancy pattern as Layer-1 evals at
first activation.

──────────────────────────────────────────────────────────────────────────────
Sidecar conventions (all sidecars live alongside the prod_snapshot in
``tests/fixtures/agent_evals/template_text/prod_snapshots/``):

``.transcript.json`` (REQUIRED for eval to run — gate is dormant without it):
  {
      "transcript_words": [
          {"text": "It's",   "start_s": 0.08, "end_s": 0.42, "confidence": 0.99},
          {"text": "not",    "start_s": 0.45, "end_s": 0.71, "confidence": 0.97},
          ...
      ]
  }
  Keys match ``TranscriptWord`` (text, start_s, end_s, confidence).
  Produced by running the ``nova.audio.transcript`` agent on the template's
  audio track and serialising ``output.words``.

``.thresholds.json`` (OPTIONAL — relaxes or tightens hard floors for one fixture):
  {
      "reason": "...",
      "floors": {
          "precision": 0.85,
          "recall": 0.85,
          "bbox_iou": 0.70
      }
  }
  ``reason`` is required when ``floors`` is non-empty (self-documenting override).
  Unset floor keys fall back to the global defaults (0.95 / 0.95 / 0.85).
  Allowed keys: ``precision``, ``recall``, ``bbox_iou``.

``ground_truth/<stem>.json`` (REQUIRED for scoring assertions):
  Same shape as Layer-1: ``{"overlays": [...TemplateTextOverlay dicts...]}``.
  Build with ``scripts/build_text_ground_truth.py``.

──────────────────────────────────────────────────────────────────────────────
Replay mode vs live mode:

Replay (default, CI):
  Skips if no ground truth or no transcript sidecar.  When both sidecars exist,
  runs the scoring math against the snapshot's existing ``output.overlays`` (the
  Layer-1 agent's output) to verify that the scoring functions work on a known
  input.  Does NOT exercise the Layer-2 pipeline — a cassette for the multi-stage
  OCR+LLM pipeline does not make sense (the pipeline has no single raw_text to
  replay).  Scoring-math regressions are caught; pipeline regressions are not
  (that's what live mode is for).

Live (--eval-mode=live --allow-cost):
  Downloads the source video from GCS to a local temp file, invokes
  ``run_full_pipeline(local_path, transcript_words=..., slot_boundaries_s=...)``
  end-to-end, scores the result against ground truth.
  Estimated cost: ~$0.03/template (Cloud Vision OCR @ 2fps).
  Requires: STORAGE_BUCKET env var, GCS credentials, GEMINI_API_KEY (alignment +
  classification LLM calls).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture
from .runners.text_overlay_scoring import OverlayScores, score_overlays

AGENT_DIR = "template_text"

# Layer-2 evals reuse the same prod_snapshots dir as Layer-1.  The input
# file_uri + slot_boundaries_s are a strict subset of what run_full_pipeline
# needs; the extra transcript_words come from a sidecar.
FIXTURE_PATHS = [p for p in discover_fixtures(AGENT_DIR) if "ground_truth" not in p.parts]

GROUND_TRUTH_DIR = (
    Path(__file__).parent.parent / "fixtures" / "agent_evals" / AGENT_DIR / "ground_truth"
)

SNAPSHOT_DIR = (
    Path(__file__).parent.parent / "fixtures" / "agent_evals" / AGENT_DIR / "prod_snapshots"
)

# ── Hard floors (design doc: §Verification plan) ─────────────────────────────
# Layer-2 floors are substantially stricter than Layer-1 because the pipeline's
# OCR + transcript-alignment combination should be near-perfect on captions that
# are literally spoken aloud.  Relax per-fixture via a .thresholds.json sidecar.
PRECISION_HARD_FLOOR = 0.95  # matched / predicted
RECALL_HARD_FLOOR = 0.95  # matched / truth  (OverlayScores.completeness)
BBOX_IOU_HARD_FLOOR = 0.85  # mean spatial IoU over matched pairs

_L2_FLOOR_KEYS = {"precision", "recall", "bbox_iou"}


# ── Sidecar helpers ───────────────────────────────────────────────────────────


def _load_ground_truth(fixture_path: Path) -> list[dict] | None:
    """Load ``ground_truth/<stem>.json``.

    Returns the overlays list or None when no ground-truth file exists.  None
    causes the test to skip — Layer-2 evals require ground truth.
    """
    candidate = GROUND_TRUTH_DIR / f"{fixture_path.stem}.json"
    if not candidate.exists():
        return None
    data = json.loads(candidate.read_text())
    overlays = data.get("overlays")
    if not isinstance(overlays, list):
        raise ValueError(f"ground truth {candidate} missing 'overlays' list")
    return overlays


def _load_transcript(fixture_path: Path) -> list[dict] | None:
    """Load ``prod_snapshots/<stem>.transcript.json``.

    Returns the transcript_words list or None when no sidecar exists.  None
    causes the test to skip — the pipeline requires a transcript for stage E
    alignment (an empty transcript degrades output quality below the 0.95 floor).

    Sidecar shape:
        {
            "transcript_words": [
                {"text": "...", "start_s": 0.0, "end_s": 0.5, "confidence": 0.99},
                ...
            ]
        }
    """
    candidate = SNAPSHOT_DIR / f"{fixture_path.stem}.transcript.json"
    if not candidate.exists():
        return None
    data = json.loads(candidate.read_text())
    words = data.get("transcript_words")
    if not isinstance(words, list):
        raise ValueError(f"transcript sidecar {candidate} missing 'transcript_words' list")
    return words


def _load_floor_overrides(fixture_path: Path) -> dict[str, float]:
    """Load optional per-fixture hard-floor overrides from a .thresholds.json sidecar.

    Sidecar lives next to the ground-truth file:
        ``ground_truth/<stem>.thresholds.json``

    Shape:
        {
            "reason": "...",
            "floors": {"precision": 0.85, "recall": 0.85, "bbox_iou": 0.70}
        }

    ``reason`` is required when ``floors`` is non-empty so the override is
    self-documenting.  Only keys in ``_L2_FLOOR_KEYS`` are honoured.
    """
    candidate = GROUND_TRUTH_DIR / f"{fixture_path.stem}.thresholds.json"
    if not candidate.exists():
        return {}
    data = json.loads(candidate.read_text())
    floors = data.get("floors") or {}
    if not isinstance(floors, dict):
        raise ValueError(f"{candidate}: 'floors' must be an object")
    bad = set(floors) - _L2_FLOOR_KEYS
    if bad:
        raise ValueError(f"{candidate}: unknown floor keys {sorted(bad)}")
    if floors and not data.get("reason"):
        raise ValueError(f"{candidate}: 'reason' is required when 'floors' is set")
    return {k: float(v) for k, v in floors.items()}


@dataclass
class _Thresholds:
    precision_floor: float
    recall_floor: float
    bbox_iou_floor: float


def _resolve_thresholds(fixture_path: Path) -> _Thresholds:
    overrides = _load_floor_overrides(fixture_path)
    return _Thresholds(
        precision_floor=overrides.get("precision", PRECISION_HARD_FLOOR),
        recall_floor=overrides.get("recall", RECALL_HARD_FLOOR),
        bbox_iou_floor=overrides.get("bbox_iou", BBOX_IOU_HARD_FLOOR),
    )


# ── Logging ───────────────────────────────────────────────────────────────────


def _summarize_scoring(s: OverlayScores) -> str:
    return (
        f"recall={s.completeness:.2f}\t"
        f"precision={s.precision:.2f}\t"
        f"s_iou={s.mean_spatial_iou:.2f}\t"
        f"t_iou={s.mean_temporal_iou:.2f}\t"
        f"(matched {s.matched_count}/{s.truth_count}, pred={s.predicted_count})"
    )


# ── Test ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "run `python scripts/export_eval_fixtures.py --only template_text` first"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_text_overlay_pipeline_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
) -> None:
    """End-to-end Layer-2 pipeline eval for one template fixture.

    Replay mode: verifies scoring-math only (scores the Layer-1 snapshot output
    against ground truth to confirm the scoring functions work on a known input).
    Does NOT call run_full_pipeline — there is no cassette for a multi-stage
    OCR+LLM pipeline.

    Live mode: downloads the source video from GCS, invokes run_full_pipeline,
    scores the result against ground truth.  All three hard floors must pass.
    """
    if "ground_truth" in fixture_path.parts:
        pytest.skip("ground-truth files are eval inputs, not standalone fixtures")

    fixture = load_fixture(fixture_path)
    # Layer-2 evals target fixtures originally produced by the template_text agent.
    if fixture.agent != "nova.compose.template_text":
        pytest.skip(f"fixture is for {fixture.agent!r}, not 'nova.compose.template_text'")

    # ── Sidecar checks — must happen before any pipeline work ─────────────
    truth = _load_ground_truth(fixture_path)
    if truth is None:
        pytest.skip(
            f"no ground truth for {fixture_path.stem} — add ground_truth/{fixture_path.stem}.json"
        )

    transcript_words = _load_transcript(fixture_path)
    if transcript_words is None:
        pytest.skip(
            f"no transcript sidecar for {fixture_path.stem} — "
            f"add prod_snapshots/{fixture_path.stem}.transcript.json"
        )

    thresholds = _resolve_thresholds(fixture_path)

    # ── Determine predicted overlays ──────────────────────────────────────
    if eval_mode == "live":
        predicted_overlays = _run_live_pipeline(fixture, transcript_words)
    else:
        # Replay: score the Layer-1 snapshot output so scoring-math regressions
        # fail the CI.  This does NOT test Layer-2 pipeline logic.
        snapshot_output = fixture.output
        predicted_overlays = snapshot_output.get("overlays", [])
        if not isinstance(predicted_overlays, list):
            pytest.skip("snapshot output has no 'overlays' list — fixture may be malformed")

    # ── Score ─────────────────────────────────────────────────────────────
    scoring = score_overlays(predicted_overlays, truth)

    template_id = fixture.meta.get("template_id", fixture_path.stem)
    print(f"\n[layer2-eval]\t{template_id}\t{eval_mode}\t{_summarize_scoring(scoring)}")

    # ── Floor assertions (live mode only; replay is scoring-math regression) ─
    floor_failures: list[str] = []
    if eval_mode == "live":
        if scoring.precision < thresholds.precision_floor:
            floor_failures.append(
                f"precision {scoring.precision:.2f} < hard floor {thresholds.precision_floor}"
            )
        if scoring.completeness < thresholds.recall_floor:
            floor_failures.append(
                f"recall {scoring.completeness:.2f} < hard floor {thresholds.recall_floor}"
            )
        if scoring.matched_count > 0 and scoring.mean_spatial_iou < thresholds.bbox_iou_floor:
            floor_failures.append(
                f"mean_spatial_iou {scoring.mean_spatial_iou:.2f} < hard floor "
                f"{thresholds.bbox_iou_floor}"
            )

    if floor_failures:
        detail = _summarize_scoring(scoring)
        pytest.fail(
            f"\n{fixture_path.stem}: Layer-2 pipeline eval failed\n"
            f"  scoring: {detail}\n"
            f"  failures: {floor_failures}"
        )


# ── Live-mode pipeline invocation ─────────────────────────────────────────────


def _run_live_pipeline(fixture, transcript_words: list[dict]) -> list[dict]:
    """Download the source video and invoke run_full_pipeline end-to-end.

    Returns the predicted overlays as a list of dicts for scoring.

    Errors raised here propagate to the test as failures (not skips) because
    a live pipeline crash is a genuine regression, not a missing-sidecar skip.
    """
    # Late imports so collection-time import doesn't drag in GCS/OCR deps.
    from app.pipeline.text_overlay_v2.pipeline import run_full_pipeline
    from app.storage import download_to_file

    file_uri: str = fixture.input.get("file_uri", "")
    if not file_uri:
        pytest.fail("fixture input has no file_uri — cannot run live pipeline")

    slot_boundaries_raw: list = fixture.input.get("slot_boundaries_s", [])
    slot_boundaries_s = [tuple(s) for s in slot_boundaries_raw]  # list[list] → list[tuple]

    # TranscriptWord is a lightweight Pydantic model; pass raw dicts —
    # run_full_pipeline accepts them and validates internally.
    suffix = Path(file_uri).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        local_path = tmp.name

    try:
        download_to_file(file_uri, local_path)
        output = run_full_pipeline(
            local_path,
            transcript_words=transcript_words,  # type: ignore[arg-type]
            slot_boundaries_s=slot_boundaries_s,  # type: ignore[arg-type]
        )
    finally:
        Path(local_path).unlink(missing_ok=True)

    return [ov.model_dump() for ov in output.overlays]
