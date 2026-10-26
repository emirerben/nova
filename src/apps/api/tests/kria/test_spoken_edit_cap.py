"""KRI-955: spoken edits (voiceover, talk-to-camera) may run 240 s; footage edits stay at 120 s."""

from app.pipeline.best_parts import edit_cap_s
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.services.choice_questions import ChoiceCapability, _source_coverage
from app.services.length_planning import build_length_plan


def _voiceover_plan(voiceover_s: float, footage_s: float) -> dict:
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "voiceover",
        "has_voiceover": True,
        "voiceover_duration_s": voiceover_s,
    }
    media = [{"media_id": f"v{i}", "kind": "video", "duration_s": footage_s / 2} for i in range(2)]
    return strategy, build_length_plan(
        strategy, {"clip_assignments": media}, selected_ids=["v0", "v1"]
    )


def test_owner_caps() -> None:
    assert edit_cap_s("voiceover") == edit_cap_s("take") == MAX_PROPOSAL_DURATION_S == 240
    assert edit_cap_s("footage") == edit_cap_s("song") == 120


def test_voiceover_between_120_and_240_is_planned_without_conflict() -> None:
    _, plan = _voiceover_plan(200.0, 250.0)

    assert plan["conflicts"] == []


def test_voiceover_over_240_names_the_240_cap() -> None:
    strategy, plan = _voiceover_plan(250.0, 400.0)
    card = _source_coverage(strategy, None, [], ChoiceCapability(), snapshot={"length_plan": plan})

    assert card is not None
    assert "240" in card.reason
    assert "400" not in card.reason


def test_footage_edit_cap_is_unchanged() -> None:
    strategy = {"edit_format": "montage", "audio_strategy": "original_audio"}
    media = [{"media_id": f"v{i}", "kind": "video", "duration_s": 100.0} for i in range(3)]
    plan = build_length_plan(
        strategy,
        {"clip_assignments": media},
        selected_ids=["v0", "v1", "v2"],
        creator_target_s=150.0,
    )

    assert [c["code"] for c in plan["conflicts"]] == ["insufficient_capacity"]
