"""Qendresa Q3 case 2: "Keep the whole clip, don't cut it" on a one-clip phone montage.

Ported from the kit (`kria-qendresa-test/tests/bugs/test_q3_keep_whole_clip.py`). Her Q3 posts
are ONE continuous 11 s shot. The brief records the ask as `timing/global
{"keep_whole_take": true}` and she states no length. Before the fix the approval-time length plan
(`planner._enrich_best_parts_snapshot` -> `length_planning.build_length_plan`) kept the
analyser's single strongest moment (3.5-7.5 s) and the worker played exactly that.

Runs the planner's own approval-time plan, then the worker's plan from it
(`plan_unified_montage(approved_length_plan=...)`): pure functions, no model, DB or network.
Failure modes: docs/reviews/qendresa-says-done-b-failure-modes.md (case 2).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import _enrich_best_parts_snapshot
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage

CLIP_S = 11.0
# What analyze_clip returns for V3: 2-5 moments spanning >= 60 % of the clip.
MOMENTS = [
    {"start_s": 0.5, "end_s": 3.5, "energy": 5.0, "description": "places phone on floor"},
    {"start_s": 3.5, "end_s": 7.5, "energy": 8.0, "description": "sits down by sofa"},
    {"start_s": 7.5, "end_s": 11.0, "energy": 6.0, "description": "opens laptop"},
]
TEXT = "The prompt I run at the end of every Claude session"
STRATEGY = {
    "direction": "fast_montage",
    "edit_format": "montage",
    "audio_strategy": "licensed_music",
    "story_structure": ["she sits down", "she starts working"],
    "caption_style": "none",
    "pacing": "balanced",
    "target_duration_s": 11,
    "render_program": "guided",
    "selected_media_ids": [],
    "optional_treatments": [],
    "rationale": "one continuous shot",
    "pinned_texts": [{"text": TEXT, "corner": "top_center"}],
}
KEEP_WHOLE = BriefRequirement(
    id="r4",
    kind="timing",
    scope="global",
    description="keep the whole clip, don't cut it",
    facts={"keep_whole_take": True},
)
LINE = BriefRequirement(
    id="r1",
    kind="text",
    scope="global",
    literal=TEXT,
    description="this text for the whole video",
    facts={},
)
BRIEF = CreativeBrief(version=1, requirements=[LINE, KEEP_WHOLE])


def _row(media_id: str, duration_s: float, moments: list[dict]) -> dict:
    return {
        "media_id": media_id,
        "kind": "video",
        "duration_s": duration_s,
        "has_audio": True,
        "generation": "1",
        "analysis": {"best_moments": moments},
    }


def _clip(media_id: str, duration_s: float, moments: list[dict]) -> UnifiedClip:
    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"users/u/analysis-proxy-{media_id}.mp4",
        generation="1",
        duration_s=duration_s,
        width=1080,
        height=1920,
        analysis={"best_moments": moments},
    )


def _cuts(brief: CreativeBrief, clips=(("clip-1", CLIP_S, MOMENTS),)) -> list[tuple]:
    snapshot = {"clip_assignments": [_row(*clip) for clip in clips]}
    # The planner's own approval-time length plan (what approval pins).
    length_plan = _enrich_best_parts_snapshot(STRATEGY, snapshot, brief)["length_plan"]
    assert not length_plan["conflicts"], length_plan["conflicts"]
    # The worker's plan from that approved length plan.
    plan = plan_unified_montage(
        [_clip(*clip) for clip in clips],
        brief_view(brief),
        strategy=STRATEGY,
        approved_length_plan=length_plan,
    )
    return [(c.media_id, c.source_start_s, c.source_end_s) for c in plan.snapshot.fast_cuts]


def test_keep_whole_take_plays_the_whole_clip() -> None:
    cuts = _cuts(BRIEF)
    assert len(cuts) == 1, cuts
    _media, start, end = cuts[0]
    assert start <= 0.05 and end >= CLIP_S - 0.05, f"clip cut to {cuts} of {CLIP_S}s"


def test_control_a_stated_length_keeps_the_whole_clip() -> None:
    """Kit control: once the brief carries 11 s ("Make it 11 seconds") the clip plays 0-11 s."""
    brief = CreativeBrief(
        version=1,
        requirements=[
            *BRIEF.requirements,
            BriefRequirement(
                id="r9",
                kind="timing",
                scope="global",
                description="make it 11 seconds",
                facts={"duration_s": 11},
            ),
        ],
    )
    assert _cuts(brief) == [("clip-1", 0.0, 11.0)]


def test_no_keep_whole_fact_keeps_the_best_moment() -> None:
    """Without the typed fact (the words alone) the montage still takes its best moment."""
    brief = CreativeBrief(
        version=1,
        requirements=[
            LINE,
            BriefRequirement(
                id="r4",
                kind="timing",
                scope="global",
                description="keep the whole clip, don't cut it",
                facts={},
            ),
        ],
    )
    cuts = _cuts(brief)
    assert cuts != [("clip-1", 0.0, CLIP_S)], cuts


def test_two_clip_montage_is_unchanged() -> None:
    """ "Keep the whole clip" names no clip when there are two: the montage keeps its windows."""
    clips = (("clip-1", CLIP_S, MOMENTS), ("clip-2", 9.0, MOMENTS[:2]))
    with_ask = _cuts(BRIEF, clips)
    without = _cuts(CreativeBrief(version=1, requirements=[LINE]), clips)
    assert with_ask == without


def test_switch_off_keeps_the_old_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "keep_whole_take_plan_enabled", False)
    with_ask = _cuts(BRIEF)
    without = _cuts(CreativeBrief(version=1, requirements=[LINE]))
    assert with_ask == without
    assert with_ask != [("clip-1", 0.0, CLIP_S)]
