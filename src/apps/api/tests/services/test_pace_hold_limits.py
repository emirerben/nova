"""Failure-mode guards for the pace unit (the planner's pacing sets how long clips are held).

Each test is one way pace could still hurt a creator: pace must never drop a clip, never fail
or block a plan, never beat a length the creator stated, never change a plan when nothing was
said or the kill switch is off, and planning and the render must read the same rule.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.config import settings
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.services.length_planning import build_length_plan

SONG = {
    "audio_strategy": "licensed_music",
    "selected_music_section": {"start_s": 10, "end_s": 28},
}


def _media(count: int, duration_s: float = 6.0) -> dict[str, Any]:
    return {
        "clip_assignments": [
            {"media_id": f"c{i}", "duration_s": duration_s, "hook_score": 1.0} for i in range(count)
        ]
    }


def _holds(plan: dict[str, Any]) -> list[float]:
    return [round(w["source_end_s"] - w["source_start_s"], 3) for w in plan["windows"]]


def _plan(strategy: dict[str, Any], count: int = 16, **kwargs: Any) -> dict[str, Any]:
    return build_length_plan({"edit_format": "montage", **strategy}, _media(count), **kwargs)


def test_relaxed_over_a_song_section_holds_longer_and_keeps_every_clip() -> None:
    plain = _plan({**SONG, "pacing": "balanced"})
    relaxed = _plan({**SONG, "pacing": "relaxed"})
    assert relaxed["conflicts"] == []
    assert len(relaxed["windows"]) == len(plain["windows"]) == 16
    assert min(_holds(relaxed)) >= 1.4 - 1e-6
    assert sum(_holds(relaxed)) > sum(_holds(plain))
    assert relaxed["pace"]["pace"] == "relaxed"
    assert "pace" not in plain


def test_too_many_clips_for_the_floor_keeps_the_plain_plan_instead_of_failing() -> None:
    # 100 clips x 1.4 s is over the 120 s edit cap: pace cannot be met, so the plain plan stands.
    plain = _plan({"pacing": "balanced", **SONG}, count=100)
    relaxed = _plan({"pacing": "relaxed", **SONG}, count=100)
    assert relaxed["windows"] == plain["windows"]
    assert relaxed["conflicts"] == plain["conflicts"]
    assert "pace" not in relaxed


def test_a_length_the_creator_stated_beats_pace() -> None:
    plain = _plan({"pacing": "balanced"}, creator_target_s=20.0)
    for pacing in ("relaxed", "fast"):
        paced = _plan({"pacing": pacing}, creator_target_s=20.0)
        assert paced["windows"] == plain["windows"]
        assert "pace" not in paced


@pytest.mark.parametrize("switch", [True, False])
def test_silence_and_the_kill_switch_leave_the_plan_byte_identical(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    monkeypatch.setattr(settings, "pace_drives_cuts_enabled", switch)
    baseline = _plan({**SONG})
    # "balanced" is the default: silence unless the creator's words said it.
    assert _plan({**SONG, "pacing": "balanced"}) == baseline
    if not switch:
        assert _plan({**SONG, "pacing": "relaxed"}) == baseline
        assert _plan({"pacing": "fast"}) == _plan({})
    else:
        assert _plan({**SONG, "pacing": "relaxed"}) != baseline


def test_fast_over_free_footage_shortens_holds_but_keeps_a_whole_video() -> None:
    plain = _plan({"pacing": "balanced"}, count=4)
    fast = _plan({"pacing": "fast"}, count=4)
    assert fast["conflicts"] == []
    assert len(fast["windows"]) == 4
    assert max(_holds(fast)) <= 1.5 + 1e-6
    assert sum(_holds(fast)) >= 3.0
    assert sum(_holds(fast)) < sum(_holds(plain))
    # Two clips at the 1.5 s ceiling are exactly 3 s, which whole-frame rounding can undercut
    # (KRI-993 steak edit, fast pace, 2 clips -> 2.967 s -> the montage was refused): the plain
    # plan stands, and so does the render.
    short = _plan({"pacing": "fast"}, count=2)
    assert short["conflicts"] == [] and "pace" not in short
    clips = [
        UnifiedClip(media_id=f"c{i}", proxy_path=f"c{i}.mp4", generation="1", duration_s=6.0)
        for i in range(2)
    ]
    rendered = plan_unified_montage(clips, strategy={"edit_format": "montage", "pacing": "fast"})
    assert rendered.snapshot.duration_s >= 3.0


def test_planning_and_the_render_read_one_rule() -> None:
    """With no approved plan the render's windows equal the approval-time plan's."""
    for pacing in ("fast", "relaxed"):
        strategy = {"edit_format": "montage", "pacing": pacing}
        plan = build_length_plan(strategy, _media(6))
        clips = [
            UnifiedClip(media_id=f"c{i}", proxy_path=f"c{i}.mp4", generation="1", duration_s=6.0)
            for i in range(6)
        ]
        rendered = plan_unified_montage(clips, strategy=copy.deepcopy(strategy))
        cuts = [round(c.source_end_s - c.source_start_s, 2) for c in rendered.snapshot.fast_cuts]
        assert cuts == pytest.approx([round(h, 2) for h in _holds(plan)], abs=0.05), pacing
        assert rendered.snapshot.pace == pacing
