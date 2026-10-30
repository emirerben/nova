from copy import deepcopy

import pytest

from app.kria.brief import (
    BriefRequirement,
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
)
from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant
from app.kria.editor_sequence_timing import (
    SequenceTimingIntent,
    SequenceTimingPlan,
    sequence_brief_updates,
)
from app.services.authored_text_fit import _segments
from app.services.sequence_pacing import retime_sequence


def snapshot():
    return {
        "text_bars": [
            {"id": "s::sequence-0", "start_s": 0, "end_s": 1},
            {"id": "s::sequence-1", "start_s": 1, "end_s": 2},
        ]
    }


def timing_plan(mode, target):
    return SequenceTimingPlan(
        operations=({"op": "set_total_duration", "target_s": target},),
        reply="Resize the video",
        intent=SequenceTimingIntent(mode, target),
        source_id="s",
        target_ids=("s::sequence-0", "s::sequence-1"),
    )


def changed_brief(prior, mode, target):
    updates = sequence_brief_updates(prior, timing_plan(mode, target), snapshot(), "Resize")
    result = apply_updates(prior, updates, source_turn_id=f"{mode}-{target}")
    return result.model_copy(update={"version": prior.version + 1})


def test_repeated_sequence_edits_replace_only_their_current_policy_and_duration():
    unrelated = [
        BriefRequirement(id="r1", kind="audio", scope="global", description="Keep audio"),
        BriefRequirement(
            id="r2", kind="timing", scope="global", description="Hold the closing shot"
        ),
    ]
    brief = CreativeBrief(version=1, requirements=unrelated)
    for mode, duration in [("fill", 10), ("preserve", 20), ("fill", 30)]:
        brief = changed_brief(brief, mode, duration)
        assert all(requirement in brief.live() for requirement in unrelated)
        policies = [r for r in brief.live() if "sequence_timing" in r.facts]
        durations = [r for r in brief.live() if "duration_s" in r.facts]
        assert len(policies) == 1
        assert policies[0].facts["sequence_timing"]["mode"] == (
            "absolute" if mode == "preserve" else "fill"
        )
        assert len(durations) == 1
        assert durations[0].facts["duration_s"] == duration


def test_sequence_correction_keeps_requirement_ids():
    before = changed_brief(CreativeBrief(version=1), "fill", 10)
    after = changed_brief(before, "preserve", 20)
    assert {r.id for r in after.live()} == {r.id for r in before.live()}


def test_sequence_correction_refuses_a_newer_brief_version():
    before = changed_brief(CreativeBrief(version=1), "fill", 10)
    updates = sequence_brief_updates(before, timing_plan("preserve", 20), snapshot(), "Resize")
    newer = before.model_copy(update={"version": before.version + 1})
    with pytest.raises(BriefUpdateBatchError, match="version"):
        apply_updates(newer, updates, source_turn_id="stale")


def test_fixed_gaps_preserve_current_spacing_after_an_earlier_stretch():
    original = [
        {"start_s": 2.0, "end_s": 3.0},
        {"start_s": 3.5, "end_s": 5.5},
        {"start_s": 6.5, "end_s": 9.5},
    ]
    stretched = retime_sequence(original, 0, 15)
    baseline = deepcopy([r["source_params"]["sequence_timing"] for r in stretched])
    resized = retime_sequence(stretched, 0, 20, gap_policy="fixed")
    assert [b["start_s"] - a["end_s"] for a, b in zip(resized, resized[1:])] == [1, 2]
    assert [
        {
            k: value
            for k, value in r["source_params"]["sequence_timing"].items()
            if k in {"duration_s", "gap_after_s"}
        }
        for r in resized
    ] == [
        {k: value for k, value in timing.items() if k in {"duration_s", "gap_after_s"}}
        for timing in baseline
    ]


def fill_requirement(gap_policy="scale"):
    return BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description="Fill the video with this word sequence",
        facts={
            "sequence_timing": {
                "source_id": "s",
                "target_ids": ["s::sequence-0", "s::sequence-1"],
                "mode": "fill",
                "gap_policy": gap_policy,
            }
        },
    )


def text_rows(end):
    return [
        {"id": "s::sequence-0", "text": "one", "start_s": 0.0, "end_s": 4.0},
        {"id": "s::sequence-1", "text": "two", "start_s": 4.0, "end_s": end},
    ]


def test_phone_receipt_uses_crossfade_output_duration():
    slots = [
        {"duration_s": 5, "transition_after": "crossfade", "transition_duration_s": 0.3},
        {"duration_s": 5},
    ]
    canonical = _segments(slots)[-1]["output_end_s"]
    assert canonical == pytest.approx(9.7)
    facts = plan_facts_from_phone_variant(
        {"timeline_slots": slots, "text_elements": text_rows(canonical)}
    )
    assert check_requirement(fill_requirement(), facts).status == "met"


def test_close_gap_policy_rejects_a_sequence_with_a_gap():
    rows = text_rows(10)
    rows[1]["start_s"] = 8.0
    facts = plan_facts_from_phone_variant(
        {"timeline_slots": [{"duration_s": 10}], "text_elements": rows}
    )
    assert check_requirement(fill_requirement("close"), facts).status == "partial"


def test_sequence_receipt_rejects_a_removed_final_word():
    rows = text_rows(10)
    rows[-1]["removed"] = True
    facts = plan_facts_from_phone_variant(
        {"timeline_slots": [{"duration_s": 10}], "text_elements": rows}
    )
    assert check_requirement(fill_requirement(), facts).status == "partial"


def test_structured_timing_reconciliation_keeps_unrelated_model_updates():
    from app.kria.brief import BriefUpdate
    from app.kria.editor_sequence_timing import merge_sequence_brief_updates

    prior = CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(id="r1", kind="audio", scope="global", description="Keep audio"),
        ],
    )
    planned = [
        BriefUpdate(
            kind="timing",
            scope="global",
            description="Fill the video",
            facts={"editor_timing_only": True},
        ),
        BriefUpdate(kind="style", scope="global", description="Use red text"),
    ]
    updates = merge_sequence_brief_updates(
        prior, planned, timing_plan("fill", 20), snapshot(), "Fill and use red"
    )
    final = apply_updates(prior, updates, source_turn_id="turn")
    assert next(req for req in final.live() if req.id == "r1") == prior.live()[0]
    assert len([req for req in final.live() if req.description == "Use red text"]) == 1
    policy = next(req for req in final.live() if req.facts.get("sequence_timing"))
    assert policy.id == "r2"
    assert policy.facts["sequence_timing"]["target_ids"] == list(timing_plan("fill", 20).target_ids)
    assert len([req for req in final.live() if req.facts.get("duration_s") == 20]) == 1


def test_compiled_duration_replaces_model_guess_without_duplicate_requirement():
    from app.kria.brief import BriefUpdate
    from app.kria.editor_sequence_timing import merge_sequence_brief_updates

    prior = CreativeBrief(
        version=4,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Video length",
                facts={"duration_s": 30, "editor_video_duration": True},
            ),
        ],
    )
    planned = [
        BriefUpdate(
            operation="change",
            target_requirement_id="r1",
            expected_version=4,
            description="Video length",
            facts={"duration_s": 30},
        )
    ]
    updates = merge_sequence_brief_updates(
        prior, planned, timing_plan("fill", 20), snapshot(), "Fill restored video"
    )
    final = apply_updates(prior, updates, source_turn_id="turn")
    durations = [req for req in final.live() if req.facts.get("duration_s") is not None]
    assert [(req.id, req.facts["duration_s"]) for req in durations] == [("r1", 20)]
