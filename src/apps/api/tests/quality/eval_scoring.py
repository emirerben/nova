"""Scoring quality evaluation — LAUNCH GATE.

Loads ≥20 human-rated fixture clips from tests/fixtures/eval_set/.
Asserts recall@3 >= 0.70 (human-chosen clip appears in Nova's top 3 in ≥70% of cases).

Each fixture in eval_set/ is a directory with:
  raw.mp4         — source video
  ground_truth.json — {"best_start_s": float, "best_end_s": float, "notes": str}

Run: pytest tests/quality/ -v
"""

import json
from pathlib import Path

import pytest

EVAL_SET_DIR = Path(__file__).parent.parent / "fixtures" / "eval_set"
RECALL_AT_3_THRESHOLD = 0.70
OVERLAP_THRESHOLD_S = 5.0  # clip is "correct" if it overlaps with ground truth by ≥5s


def load_eval_cases() -> list[dict]:
    """Load all eval cases from eval_set directory."""
    cases = []
    if not EVAL_SET_DIR.exists():
        return cases
    for fixture_dir in sorted(EVAL_SET_DIR.iterdir()):
        gt_path = fixture_dir / "ground_truth.json"
        video_path = fixture_dir / "raw.mp4"
        if gt_path.exists() and video_path.exists():
            with open(gt_path) as f:
                gt = json.load(f)
            cases.append({
                "video_path": str(video_path),
                "best_start_s": gt["best_start_s"],
                "best_end_s": gt["best_end_s"],
                "notes": gt.get("notes", ""),
            })
    return cases


@pytest.mark.skipif(
    not EVAL_SET_DIR.exists() or len(list(EVAL_SET_DIR.iterdir())) < 20,
    reason="eval_set requires ≥20 human-rated fixtures — see tests/fixtures/eval_set/README.md",
)
def test_recall_at_3_meets_launch_threshold():
    """LAUNCH GATE: ≥70% of human-chosen clips must appear in Nova's top 3."""

    from app.pipeline import probe as probe_mod
    from app.pipeline import scene_detect
    from app.pipeline import transcribe as transcribe_mod
    from app.pipeline.score import TOP_N, select_candidates

    cases = load_eval_cases()
    assert len(cases) >= 20, f"Need ≥20 eval fixtures, found {len(cases)}"

    hits = 0
    for case in cases:
        video_path = case["video_path"]
        gt_start = case["best_start_s"]
        gt_end = case["best_end_s"]

        video_probe = probe_mod.probe_video(video_path)
        transcript = transcribe_mod.transcribe(video_path)
        cuts = scene_detect.detect_scenes(video_path)
        candidates = select_candidates(video_probe, transcript, cuts)
        top3 = candidates[:TOP_N]

        # Check if any top-3 candidate overlaps with ground truth by ≥ OVERLAP_THRESHOLD_S
        for candidate in top3:
            overlap = min(candidate.end_s, gt_end) - max(candidate.start_s, gt_start)
            if overlap >= OVERLAP_THRESHOLD_S:
                hits += 1
                break

    recall = hits / len(cases)
    assert recall >= RECALL_AT_3_THRESHOLD, (
        f"Recall@3 = {recall:.2%} — below launch threshold of {RECALL_AT_3_THRESHOLD:.0%}. "
        f"({hits}/{len(cases)} cases). Review scoring weights in config.py."
    )
