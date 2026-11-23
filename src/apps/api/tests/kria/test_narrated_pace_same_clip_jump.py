"""The pace receipt counts a jump inside one clip as a cut (#2382 review, Tadic, 2026-11-18).

`narrated_render_checks._shots` turned adjacent steps with the same media id into one shot. Two
windows of one clip that are not back to back in the source (the pace fill reuses a clip, or a
clip comes back on its word) are a visible cut on the phone, so the receipt could say "12 shots"
for an edit that changes the picture 20 times, and call a request that was followed "Partly".

Ways this can fail, written before the code:

 J1 a jump inside one clip (the next step starts somewhere else in the same source) is still
    merged into the shot before it, so the shot count is too low and a followed pace reads
    "Partly" (the gap the review named).
 J2 one clip that keeps playing across a step boundary (the next step starts where the last one
    stopped in the source) is counted as two shots, so a long held shot hides behind a split
    and a missed pace reads "Done".
 J3 the in-points the record rounds to 1 ms (and the 0 a row omits) make a continuous step look
    like a jump, or a replay from the clip's start look continuous.
 J4 steps of different clips stop being cuts, or the recorded Tahini render (12 different
    clips) changes its verdict.
 J5 with NARRATED_CUE_RECEIPTS_ENABLED off anything is judged (it must stay unjudged).

Every case goes through the real record reader (`plan_facts_from_narrated_alignment`) and the
real requirement check, as the finished-video review does. No model call.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_narrated_alignment

PACE = BriefRequirement(
    id="r16",
    kind="style",
    scope="global",
    description="Change the picture about once a second; reuse a clip if needed.",
    facts={"video_reuse_allowed": True, "approximate_cut_duration_s": 1},
)


def _row(i: int, media: str, start: float, end: float, source: float) -> dict:
    row = {
        "step_id": f"seg_{i}",
        "media_id": media,
        "clip": media,
        "labels": [],
        "placed": [],
        "start_s": start,
        "end_s": end,
        "text": "",
    }
    if source > 0:  # the worker omits a 0 in-point, as `_narrated_alignment_record` does
        row["source_start_s"] = round(source, 3)
    return row


def _facts(rows: list[dict]) -> PlanFacts:
    steps = plan_facts_from_narrated_alignment(
        {"steps": rows, "ordering_basis": "spoken_word_alignment"}
    ).narrated_steps
    return dataclasses.replace(PlanFacts(rendered_variant=True), narrated_cue_steps=steps)


def _pace(rows: list[dict]):
    return check_requirement(PACE, _facts(rows))


@pytest.fixture(autouse=True)
def _receipts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", True)


def test_j1_six_one_second_jumps_inside_one_clip_are_six_shots() -> None:
    """Six 1 s steps of one 8 s clip, each from another part of it: the picture changes every
    second, so the pace is met."""
    sources = [0.0, 4.0, 1.5, 6.0, 2.5, 7.0]
    rows = [_row(i, "c0", float(i), float(i + 1), src) for i, src in enumerate(sources)]

    receipt = _pace(rows)

    assert receipt.status == "met", receipt.reason
    assert receipt.reason.startswith("6 shots in 6.0 s, about one every 1.0 s")


def test_j2_control_one_clip_playing_on_across_six_steps_is_one_shot() -> None:
    """The same six steps, each starting where the last one stopped: one held 6 s shot, so the
    1 s pace is missed (the contrast the review asked for)."""
    rows = [_row(i, "c0", float(i), float(i + 1), float(i)) for i in range(6)]

    receipt = _pace(rows)

    assert receipt.status == "partial", receipt.reason
    assert receipt.reason.startswith("1 shots in 6.0 s, about one every 6.0 s, not every 1.0 s")


def test_j3_rounding_keeps_a_continuous_step_continuous_and_a_replay_a_jump() -> None:
    continuous = [_row(0, "c0", 0.0, 1.0004, 0.0), _row(1, "c0", 1.0004, 2.0, 1.0)]
    replay = [_row(0, "c0", 0.0, 1.0, 0.0), _row(1, "c0", 1.0, 2.0, 0.0)]

    assert _pace(continuous).reason.startswith("1 shots in 2.0 s")
    assert _pace(replay).reason.startswith("2 shots in 2.0 s")


def test_j4_different_clips_are_cuts_whatever_their_in_points() -> None:
    rows = [_row(i, f"c{i}", float(i), float(i + 1), float(i)) for i in range(6)]

    receipt = _pace(rows)

    assert receipt.status == "met"
    assert receipt.reason.startswith("6 shots in 6.0 s")


def test_j5_switch_off_judges_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", False)
    rows = [_row(i, "c0", float(i), float(i + 1), 0.0) for i in range(6)]

    receipt = _pace(rows)

    assert receipt.verification != "checked"
