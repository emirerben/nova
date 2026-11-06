"""Qendresa case 2 (receipt half): "Keep the whole clip, don't cut it" on a one-clip montage.

Ported from the Qendresa kit (`test_q3_keep_whole_clip.py`). Her Q3 post is ONE continuous,
uncut shot (11 s). The brief records the ask as `timing/global {"keep_whole_take": true}` and she
states no number. The approval-time length plan keeps only the analyser's highest-energy moment
(3.5-7.5 s), the worker plans exactly that window, and the keep-whole ask got the neutral "I can't
confirm this draft keeps your whole take." (unchecked), so nothing told her the clip is cut.

Group B makes the plan keep the clip; this receipt must be right either way: a judged answer
from the plan's own windows, before approval and after. Failure modes:
`docs/reviews/says-done-receipts-failure-modes.md` (case 2). Pure planner/length/receipt
functions: no model, DB or network.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_judged,
    needs_creator_choice,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
    with_planned_cuts,
)
from app.kria.planner import _enrich_best_parts_snapshot
from app.kria.reply_language import reply_language_for
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage

CLIP_S = 11.0
# What analyze_clip returns for V3 (sets the phone down, sits by the sofa, opens the laptop).
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
ELEVEN_SECONDS = BriefRequirement(
    id="r9",
    kind="timing",
    scope="global",
    description="make it 11 seconds",
    facts={"duration_s": 11},
)
SNAPSHOT = {
    "clip_assignments": [
        {
            "media_id": "clip-1",
            "kind": "video",
            "duration_s": CLIP_S,
            "has_audio": True,
            "generation": "1",
            "analysis": {"best_moments": MOMENTS},
        }
    ]
}


def _brief(*extra: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=[LINE, KEEP_WHOLE, *extra])


def _length_plan(brief: CreativeBrief) -> dict:
    enriched = _enrich_best_parts_snapshot(STRATEGY, SNAPSHOT, brief)
    length_plan = enriched["length_plan"]
    assert not length_plan["conflicts"], length_plan["conflicts"]
    return length_plan


def _plan(brief: CreativeBrief):
    clip = UnifiedClip(
        media_id="clip-1",
        proxy_path="users/u/analysis-proxy-clip-1.mp4",
        generation="1",
        duration_s=CLIP_S,
        width=1080,
        height=1920,
        analysis={"best_moments": MOMENTS},
    )
    return plan_unified_montage(
        [clip], brief_view(brief), strategy=STRATEGY, approved_length_plan=_length_plan(brief)
    )


def _keep(brief: CreativeBrief, facts: PlanFacts):
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    return next(r for r in receipts if r.requirement_id == KEEP_WHOLE.id)


def test_keep_whole_clip_plays_the_whole_clip_or_the_draft_says_it_is_cut() -> None:
    """The kit's test: either the plan plays 0-11 s, or the receipt is a judged miss."""
    brief = _brief()
    plan = _plan(brief)
    cuts = [(c.source_start_s, c.source_end_s) for c in plan.snapshot.fast_cuts]
    whole = len(cuts) == 1 and cuts[0][0] <= 0.05 and cuts[0][1] >= CLIP_S - 0.05
    keep = _keep(brief, plan_facts_from_unified_montage(plan.record()))
    told = keep.verification == "checked" and keep.status != "met" and is_judged(KEEP_WHOLE, keep)
    assert whole or told, (
        f"clip cut to {cuts} (total {plan.duration_s}s of {CLIP_S}s) and the keep-whole "
        f"receipt is {keep.status}/{keep.verification}: {keep.reason!r}"
    )
    if whole:
        assert keep.status == "met", keep.reason
    else:
        kept = sum(end - start for start, end in cuts)
        assert f"{kept:g} s of your {CLIP_S:g} s clip" in keep.reason


def test_draft_before_approval_says_the_clip_is_cut() -> None:
    """2.1 / 2.5: the approval-time draft judges the length plan's windows, so the creator
    hears it before she approves, as a choice of hers (not a format limit)."""
    brief = _brief()
    length_plan = _length_plan(brief)
    windows = [(w["source_start_s"], w["source_end_s"]) for w in length_plan["windows"]]
    keep = _keep(
        brief, with_planned_cuts(plan_facts_from_strategy(STRATEGY), {"length_plan": length_plan})
    )
    if windows == [(0.0, CLIP_S)]:
        assert keep.status == "met", keep.reason  # group B's plan fix keeps the clip
        return
    assert keep.status == "partial" and is_judged(KEEP_WHOLE, keep), keep
    assert needs_creator_choice(keep)
    kept = sum(end - start for start, end in windows)
    assert keep.reason == f"This draft keeps {kept:g} s of your {CLIP_S:g} s clip, so it's cut."


def test_whole_clip_kept_is_met() -> None:
    """2.2: with "make it 11 seconds" the same harness keeps 0-11 s; the receipt is met,
    before approval and on the worker's record."""
    brief = _brief(ELEVEN_SECONDS)
    plan = _plan(brief)
    assert [(c.source_start_s, c.source_end_s) for c in plan.snapshot.fast_cuts] == [(0.0, 11.0)]
    assert _keep(brief, plan_facts_from_unified_montage(plan.record())).status == "met"
    draft = with_planned_cuts(
        plan_facts_from_strategy(STRATEGY), {"length_plan": _length_plan(brief)}
    )
    assert _keep(brief, draft).status == "met"


def _record(windows: list[tuple[float, float]]) -> dict:
    return {
        "clip_ids": ["clip-1"],
        "duration_s": sum(end - start for start, end in windows),
        "clip_durations_s": {"clip-1": CLIP_S},
        "best_parts": [
            {"media_id": "clip-1", "source_start_s": start, "source_end_s": end}
            for start, end in windows
        ],
    }


def test_split_windows_covering_the_clip_are_met() -> None:
    """2.3: two windows that together cover the clip keep it whole; overlapping ones count
    once."""
    brief = _brief()
    whole = plan_facts_from_unified_montage(_record([(0.0, 5.5), (5.5, 11.0)]))
    assert _keep(brief, whole).status == "met"
    overlap = plan_facts_from_unified_montage(_record([(3.0, 7.0), (3.0, 7.0)]))
    keep = _keep(brief, overlap)
    assert keep.status == "partial"
    assert "keeps 4 s of your 11 s clip" in keep.reason


def test_turkish_reason_for_cut_take() -> None:
    with reply_language_for("tr"):
        keep = _keep(_brief(), plan_facts_from_unified_montage(_record([(3.5, 7.5)])))
    assert keep.status == "partial"
    assert keep.reason == "Bu taslak 11 sn'lik klibinin 4 sn'sini tutuyor, yani klip kesiliyor."
