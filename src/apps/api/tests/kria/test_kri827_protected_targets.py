"""KRI-827: what text stays off, and where -- only the creator puts an object on the video.

The model records the ask as a structured fact (`keep_text_off`, KRI-667 language policy);
WHERE an object is comes only from the creator's own mark. F12: model output (a requirement
description, a model event, a fact without a mark) never becomes an object region; the
client never stamps the clip's generation. Replays Merope T1's real brief (prod thread
32A5B608, 2026-10-21).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.protected_targets import (
    ProtectedRegionSelection,
    ProtectedTarget,
    fold_protected_regions,
    protected_targets,
    selection_for,
)

BOX = (0.3, 0.42, 0.7, 0.64)
FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri793_t1_caro"
        / "t1_brief_and_speech.json"
    ).read_text(encoding="utf-8")
)
T1_BRIEF = CreativeBrief.model_validate(FIXTURE["brief"])
# What the v76 brief section teaches for "No text on her face or on the plate".
KEEP_OFF = BriefRequirement(
    id="r8",
    kind="style",
    scope="global",
    description="No text on her face or on the plate",
    facts={"keep_text_off": [{"kind": "face"}, {"kind": "object", "label": "plate"}]},
)


def _mark(label: str = "plate") -> ProtectedRegionSelection:
    return ProtectedRegionSelection(label=label, media_id="clip-1", source_t_s=0.0, box=BOX)


def test_merope_asks_to_keep_text_off_her_face_and_the_plate():
    brief = T1_BRIEF.model_copy(update={"requirements": [*T1_BRIEF.requirements, KEEP_OFF]})
    assert protected_targets(brief) == (
        ProtectedTarget("face", "face", ("r8",)),
        ProtectedTarget("object", "plate", ("r8",)),
    )


def test_a_description_alone_protects_nothing():
    """The stored T1 brief (before v76) says it only in r6's description: nothing is read
    from that model-written prose."""
    assert protected_targets(T1_BRIEF) == ()


def test_the_creators_mark_is_an_ask_on_its_own():
    assert protected_targets(None, [_mark("cup")]) == (ProtectedTarget("object", "cup", ()),)


def test_a_mark_only_stands_for_the_thing_it_names():
    plate = ProtectedTarget("object", "Plate")
    assert selection_for(plate, [_mark("cup"), _mark("plate")]).label == "plate"
    assert selection_for(plate, [_mark("cup")]) is None


def test_malformed_facts_name_nothing():
    junk = KEEP_OFF.model_copy(
        update={"facts": {"keep_text_off": ["plate", {"kind": "object"}, {"kind": "logo"}]}}
    )
    assert protected_targets(CreativeBrief(version=1, requirements=[junk])) == ()


def test_the_client_cannot_stamp_a_generation() -> None:
    with pytest.raises(ValidationError):
        SubmitTurnBody(
            message="This is the plate",
            client_event_id="mark-x",
            expected_thread_revision=1,
            protected_region={
                "label": "plate",
                "media_id": "clip-1",
                "source_t_s": 0.0,
                "box": BOX,
                "generation": "forged",
            },
        )


def test_only_creator_turns_author_marks() -> None:
    mark = {"label": "plate", "media_id": "clip-1", "source_t_s": 0.0, "box": list(BOX)}
    events = [
        ("assistant", {"protected_region": mark}),
        ("system", {"protected_region": mark}),
        ("user", {"protected_region": {**mark, "box": [0.0, 0.0, 0.001, 0.001]}}),  # too small
    ]
    assert fold_protected_regions(events) == []


def test_re_marking_on_the_speaking_clip_wins_over_an_older_mark_elsewhere() -> None:
    """The creator marks the plate on A, then B by mistake, then A again: the render of A
    uses A's latest mark, never "you marked it on another clip"."""
    a = {"label": "plate", "media_id": "clip-a", "source_t_s": 0.0, "box": list(BOX)}
    b = {**a, "media_id": "clip-b"}
    a2 = {**a, "source_t_s": 1.5}
    marks = fold_protected_regions([("user", {"protected_region": m}) for m in (a, b, a2)])
    selections = [ProtectedRegionSelection.model_validate(m) for m in marks]
    target = ProtectedTarget("object", "plate", ("r8",))
    assert selection_for(target, selections).source_t_s == 1.5  # latest overall is A again
    chosen = selection_for(target, selections, media_id="clip-a")
    assert (chosen.media_id, chosen.source_t_s) == ("clip-a", 1.5)


def test_another_ask_on_the_same_requirement_is_still_judged() -> None:
    """ "Brighten the clips and no text on the plate" in one requirement: the plate measured
    clear never certifies the brightening the render cannot do."""
    from app.kria.brief_checks import PlanFacts, check_requirement

    req = KEEP_OFF.model_copy(update={"facts": {**KEEP_OFF.facts, "footage_light": "brighter"}})
    clear = PlanFacts(protected_text=(("r8", "clear", ("face", "plate")),))
    assert check_requirement(req, clear).status != "met"
    # The keep-off ask alone, measured clear, is met; measured unverified, it is not.
    assert check_requirement(KEEP_OFF, clear).status == "met"
    unverified = PlanFacts(protected_text=(("r8", "unverified", ("plate",)),))
    assert check_requirement(KEEP_OFF, unverified).status == "partial"


def test_a_face_the_sampler_never_saw_is_not_called_clear() -> None:
    """Face sampler failed / timed out -> the fallback box was protected: best effort, so
    the face target is unverified, never clear (and a placement error is never clear)."""
    from app.pipeline.phone_protected_text import ProtectedPlan, protected_text_receipt

    plan = ProtectedPlan(
        targets=(ProtectedTarget("face", "face", ("r8",)),),
        target_rows=({"kind": "face", "label": "face", "requirement_ids": ["r8"]},),
        protect_all_faces=True,
    )
    seen = {"status": "moved", "faces": "detected", "face_collision": False}
    assert protected_text_receipt(plan, {"title": seen})["verdict"] == "clear"
    fallback = {**seen, "faces": "fallback"}
    assert protected_text_receipt(plan, {"title": fallback})["verdict"] == "unverified"
    error = {"status": "error", "object_collision": True}
    assert protected_text_receipt(plan, {"title": seen, "closing": error})["verdict"] != "clear"
    hit = {**seen, "face_collision": True}
    assert protected_text_receipt(plan, {"title": hit})["verdict"] == "collision"
