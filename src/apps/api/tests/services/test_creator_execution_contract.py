import copy
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import canonical_context_hash
from app.services.creator_execution_contract import (
    execution_identity,
    narration_matches_item,
    requests_guided_voiceover,
    validate_execution_binding,
)


def binding():
    strategy = {
        "execution_contract": "guided_voiceover_v1",
        "render_program": "guided",
        "audio_strategy": "voiceover",
        "selected_media_ids": ["video-1", "asset-photo-1"],
    }
    edit_plan = {
        "strategy": strategy,
        "manifest_hash": "m" * 64,
        "context_hash": "c" * 64,
    }
    active = {"edit_plan": edit_plan, "plan_hash": canonical_context_hash(edit_plan)}
    snapshot = {
        "execution_contract": "guided_voiceover_v1",
        "approved_proposal": {
            "narration": {"gcs_path": "users/u/take.mp3", "generation": "123", "duration_s": 44.7}
        },
        "creator_execution_identity": execution_identity(active, strategy),
    }
    return strategy, snapshot


def test_legacy_narration_is_not_upgraded_by_a_feature_flag():
    assert not requests_guided_voiceover(
        {"render_program": "native", "audio_strategy": "voiceover"}
    )
    assert validate_execution_binding(None, {}, "users/u/take.mp3") is False


def test_confirmed_visual_plan_and_voiceover_survive_dispatch():
    strategy, snapshot = binding()
    assert validate_execution_binding(snapshot, strategy, "users/u/take.mp3") is True


def test_removing_strategy_marker_cannot_downgrade_confirmed_voiceover():
    strategy, snapshot = binding()
    strategy.pop("execution_contract")
    with pytest.raises(ValueError):
        validate_execution_binding(snapshot, strategy, "users/u/take.mp3")


@pytest.mark.parametrize("change", ["missing", "audio", "strategy", "hash", "manifest"])
def test_confirmed_plan_cannot_silently_downgrade_or_change(change):
    strategy, original = binding()
    snapshot = copy.deepcopy(original)
    if change == "missing":
        snapshot = None
    elif change == "audio":
        snapshot["approved_proposal"]["narration"]["gcs_path"] = "users/u/replaced.mp3"
    elif change == "strategy":
        strategy = {**strategy, "selected_media_ids": ["video-1"]}
    elif change == "hash":
        snapshot["creator_execution_identity"]["edit_plan_hash"] = "0" * 64
    else:
        snapshot["creator_execution_identity"]["manifest_hash"] = "0" * 64
    with pytest.raises(ValueError):
        validate_execution_binding(snapshot, strategy, "users/u/take.mp3")


@pytest.mark.parametrize(
    "field,value",
    [
        ("voiceover_gcs_path", "users/u/new.mp3"),
        ("voiceover_generation", "124"),
        ("voiceover_duration_s", 40),
        ("audio_mode", "original"),
    ],
)
def test_same_path_replacement_duration_and_audio_changes_invalidate_approval(field, value):
    _, snapshot = binding()
    narration = snapshot["approved_proposal"]["narration"]
    item = SimpleNamespace(
        voiceover_gcs_path="users/u/take.mp3",
        voiceover_generation="123",
        voiceover_duration_s=44.7,
        audio_mode="voiceover",
    )
    assert narration_matches_item(narration, item)
    setattr(item, field, value)
    assert not narration_matches_item(narration, item)


def test_legacy_label_migration_preserves_original_approval_hashes():
    from app.agents._schemas.creator_agent import CreativeStrategy

    legacy, snapshot = binding()
    legacy.update(participant_labels="single_subject", score_labels=True, sport_labels=True)
    edit_plan = snapshot["creator_execution_identity"]["edit_plan"]
    active = {"edit_plan": edit_plan, "plan_hash": canonical_context_hash(edit_plan)}
    migrated = CreativeStrategy.model_validate(legacy).model_dump(mode="json", exclude_none=True)
    identity = execution_identity(active, migrated)
    assert identity["edit_plan"]["strategy"] == legacy
    assert identity["edit_plan_hash"] == canonical_context_hash(edit_plan)
    snapshot["creator_execution_identity"] = identity
    assert validate_execution_binding(snapshot, migrated, "users/u/take.mp3")
    modified = {**migrated, "clip_intents": []}
    with pytest.raises(ValueError, match="differs"):
        execution_identity(active, modified)
    with pytest.raises(ValueError, match="changed"):
        validate_execution_binding(snapshot, modified, "users/u/take.mp3")
