"""Voice eval for the camera-roll idea agent (diary voice vs viewer-directed ideas).

  pytest tests/evals/test_camera_roll_voice_evals.py -v                # replay, free, CI
  ... --with-judge                                                     # + Claude judge (paid)
  ... -k live --eval-mode=live <approval flags>                        # live agent (paid)

Prefer `scripts/run_camera_roll_voice_eval.py`, which prints the plan and cost first.
Replay uses two hand-written responses (`camera_roll_voice_samples.py`) to prove the checks
separate diary voice from a viewer-directed register. It does not measure the agent.
"""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.camera_roll_stories import CameraRollStoriesAgent

from . import camera_roll_voice_samples as samples
from .camera_roll_voice_fixture import PLACE, VARIANTS, build_input, candidates
from .runners.camera_roll_voice import analyze_stories, composite, score_run
from .runners.eval_runner import CassetteModelClient, build_eval_run_context

AGENT = "nova.media.camera_roll_stories"
BASELINE_DIR = Path(__file__).parent / "baselines" / "camera_roll_voice"


def _replay(sample: dict):
    value = build_input()
    agent = CameraRollStoriesAgent(CassetteModelClient(samples.raw(sample)))
    output = agent.run(
        value.model_dump(), ctx=build_eval_run_context("camera-roll-voice/replay", is_live=False)
    )
    return value, output.model_dump()


def test_fixture_is_a_no_gps_mix_of_photos_and_clips():
    pool = candidates()
    assert len(pool) == 12
    assert {c.media for c in pool} == {"still", "video"}
    assert len({c.source for c in pool}) == 12
    for place_word in ("cunda", "turkey", "aegean"):
        assert all(place_word not in c.text.lower() for c in pool)


def test_diary_sample_is_flagged_and_social_sample_is_not():
    _, diary = _replay(samples.DIARY)
    _, social = _replay(samples.SOCIAL)
    diary_report = analyze_stories(diary["stories"], place=PLACE)
    social_report = analyze_stories(social["stories"], place=PLACE)

    assert diary_report.rate("diary_recap") == 1.0
    assert social_report.rate("diary_recap") == 0.0
    assert diary_report.rate("names_place") == 0.0
    assert social_report.rate("names_place") == 1.0
    assert all(idea.filler for idea in diary_report.ideas)
    assert not any(idea.filler for idea in social_report.ideas)


def test_place_check_is_skipped_when_no_place_is_in_context():
    _, diary = _replay(samples.DIARY)
    summary = analyze_stories(diary["stories"], place=None).summary()
    assert summary["missing_place_rate"] is None
    assert summary["diary_recap_rate"] == 1.0


def test_a_recap_marker_with_a_viewer_claim_is_not_diary():
    story = {
        "title": "Why I would skip the beach",
        "content_plan": {"opening_text": "I'd skip the beach: you get this instead"},
    }
    (idea,) = analyze_stories([story]).ideas
    assert idea.recap_markers and idea.viewer_directed and not idea.diary_recap


def test_judge_separates_the_two_samples(judge_for, with_judge):
    if not with_judge:
        pytest.skip("judge scoring needs --with-judge")
    judge = judge_for(AGENT)
    value, diary = _replay(samples.DIARY)
    _, social = _replay(samples.SOCIAL)
    ours = score_run(diary, place=PLACE, agent_input=value.model_dump(), judge=judge)["judge"]
    target = score_run(social, place=PLACE, agent_input=value.model_dump(), judge=judge)["judge"]
    assert ours["scores"]["diary_voice"] >= target["scores"]["diary_voice"] + 2, ours
    assert target["scores"]["viewer_value"] >= ours["scores"]["viewer_value"] + 1, target
    assert target["composite"] > ours["composite"] and composite(ours["scores"]) is not None


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001 - provenance only
        return "unknown"


def test_live_baseline(eval_mode, live_model_client, with_judge, judge_for):
    """Paid. Runs the CURRENT agent on each fixture variant and saves outputs + scores."""
    if eval_mode != "live":
        pytest.skip("live baseline needs --eval-mode=live (see the runner script)")
    label = os.environ.get("NOVA_VOICE_EVAL_LABEL", "").strip()
    assert label, "NOVA_VOICE_EVAL_LABEL is required to name the baseline file"
    runs = int(os.environ.get("NOVA_VOICE_EVAL_RUNS", "1"))
    wanted = {v for v in os.environ.get("NOVA_VOICE_EVAL_VARIANTS", "").split(",") if v}
    judge = judge_for(AGENT) if with_judge else None
    spec = CameraRollStoriesAgent.spec

    record = {
        "label": label,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "agent": spec.name,
        "prompt_version": spec.prompt_version,
        "model": spec.model,
        "judged": judge is not None,
        "variants": {},
    }
    for variant in VARIANTS:
        if wanted and variant.name not in wanted:
            continue
        value = variant.build()
        agent = CameraRollStoriesAgent(live_model_client)
        results = []
        for n in range(runs):
            ctx = build_eval_run_context(f"camera-roll-voice/{variant.name}/{n}", is_live=True)
            try:
                output = agent.run(value.model_dump(), ctx=ctx).model_dump()
            except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
                results.append({"error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            results.append(
                score_run(output, place=variant.place, agent_input=value.model_dump(), judge=judge)
            )
        record["variants"][variant.name] = {
            "description": variant.description,
            "query": variant.query,
            "runs": [r for r in results if "error" not in r],
            "errors": [r["error"] for r in results if "error" in r],
        }
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = BASELINE_DIR / f"{label}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"baseline written: {path}")
    assert any(v["runs"] for v in record["variants"].values()), "no run produced output"
