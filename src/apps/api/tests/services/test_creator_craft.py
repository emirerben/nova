"""Core Creator craft bundle contracts and deterministic compilation."""

import uuid

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreatorCraftBundle
from app.services.creator_craft import (
    CreatorCraftValidationError,
    build_core_craft_editor_commit,
    build_media_overlay_craft_editor_commit,
    craft_preview,
)


def _target() -> dict:
    return {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": str(uuid.uuid4()),
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-1",
        "expected_revision": 4,
        "expected_ownership_epoch": 2,
    }


def _bundle(*commands: dict) -> CreatorCraftBundle:
    target = _target()
    command_values = [{**target, **command} for command in commands]
    return CreatorCraftBundle(
        session_id=str(uuid.uuid4()),
        idempotency_key="craft-event-1",
        **target,
        commands=command_values,
    )


def test_bundle_pins_every_command_to_one_opaque_target() -> None:
    bundle = _bundle({"command": "set_caption_style", "caption_style": "word"})
    assert bundle.commands[0].expected_job_id == bundle.expected_job_id

    with pytest.raises(ValidationError, match="does not pin bundle expected_generation_id"):
        target = _target()
        CreatorCraftBundle(
            session_id=str(uuid.uuid4()),
            idempotency_key="craft-event-2",
            **target,
            commands=[
                {
                    **target,
                    "expected_generation_id": "different-generation",
                    "command": "set_look_preset",
                    "slot_index": 0,
                    "look_preset": "none",
                }
            ],
        )

    with pytest.raises(ValidationError):
        _bundle(
            {
                "command": "set_caption_style",
                "caption_style": "word",
                "expected_job_id": "gs://private/job",
            }
        )


def test_compiler_delegates_caption_transition_and_look_to_editor_commit() -> None:
    bundle = _bundle(
        {"command": "set_caption_style", "caption_style": "word"},
        {
            "command": "set_transition",
            "boundary_index": 0,
            "transition": "crossfade",
            "duration_s": 0.2,
        },
        {"command": "set_look_preset", "slot_index": 1, "look_preset": "golden_hour"},
    )
    variant = {
        "ai_timeline": {
            "slots": [
                {"clip_index": 0, "in_s": 0, "duration_s": 2.0, "look_preset": "none"},
                {"clip_index": 1, "in_s": 0, "duration_s": 2.0, "look_preset": "none"},
            ]
        }
    }

    commit = build_core_craft_editor_commit(bundle, variant=variant)

    assert commit.base_generation == bundle.expected_generation_id
    assert commit.caption_meta is not None and commit.caption_meta.style == "word"
    assert commit.timeline_slots is not None
    assert commit.timeline_slots[0].transition_after == "crossfade"
    assert commit.timeline_slots[0].transition_duration_s == 0.2
    assert commit.timeline_slots[1].look_preset == "golden_hour"


def test_compiler_rejects_unsupported_or_stale_timeline_targets_before_write() -> None:
    bundle = _bundle(
        {
            "command": "set_transition",
            "boundary_index": 0,
            "transition": "wipe_left",
            "duration_s": 0.2,
        }
    )
    with pytest.raises(CreatorCraftValidationError, match="does not support wipe"):
        build_core_craft_editor_commit(
            bundle, variant={"ai_timeline": {"slots": [{"clip_index": 0, "duration_s": 2}]}}
        )

    look_bundle = _bundle({"command": "set_look_preset", "slot_index": 9, "look_preset": "none"})
    with pytest.raises(CreatorCraftValidationError, match="outside the timeline"):
        build_core_craft_editor_commit(
            look_bundle,
            variant={"ai_timeline": {"slots": [{"clip_index": 0, "duration_s": 2}]}},
        )


def test_preview_contains_bounded_treatment_data_only() -> None:
    bundle = _bundle({"command": "set_caption_style", "caption_style": "sentence"})
    preview = craft_preview(bundle, generation="new-generation", sections={"caption_meta": True})
    assert preview == {
        "generation": "new-generation",
        "commands": ["set_caption_style"],
        "sections": {"caption_meta": True},
        "caption_style": "sentence",
        "transitions": [],
        "looks": [],
    }


def test_media_overlay_compiler_resolves_only_server_asset_snapshot() -> None:
    bundle = _bundle(
        {"command": "set_media_overlay", "asset_id": "asset-42", "start_s": 1.25, "end_s": 3.5}
    )
    commit = build_media_overlay_craft_editor_commit(
        bundle,
        variant={"media_overlays": [{"id": "old", "kind": "image", "src_gcs_path": "users/u/old"}]},
        asset={
            "id": "42",
            "kind": "video",
            "gcs_path": "users/u/plan/item/pool/new.mp4",
            "preview_gcs_path": "users/u/plan/item/pool/new.jpg",
            "duration_s": 8.0,
        },
    )
    assert commit.base_generation == bundle.expected_generation_id
    assert commit.media_overlays is not None
    assert len(commit.media_overlays) == 2
    added = commit.media_overlays[-1]
    assert added["src_gcs_path"].startswith("users/")
    assert added["start_s"] == 1.25 and added["end_s"] == 3.5
    assert added["clip_duration_s"] == 8.0


def test_media_overlay_compiler_rejects_mixed_bundle_without_partial_commit() -> None:
    with pytest.raises(ValidationError, match="media overlay craft must be the only command"):
        _bundle(
            {"command": "set_media_overlay", "asset_id": "asset-42", "start_s": 0, "end_s": 1},
            {"command": "set_caption_style", "caption_style": "word"},
        )

    bundle = _bundle(
        {"command": "set_media_overlay", "asset_id": "asset-42", "start_s": 0, "end_s": 1}
    )
    with pytest.raises(CreatorCraftValidationError):
        build_media_overlay_craft_editor_commit(bundle, variant={}, asset={"kind": "image"})


def test_media_overlay_bundle_stages_multiple_cards_atomically() -> None:
    bundle = _bundle(
        {"command": "set_media_overlay", "asset_id": "asset-1", "start_s": 0, "end_s": 1},
        {"command": "set_media_overlay", "asset_id": "visual-2", "start_s": 2, "end_s": 3},
    )
    commit = build_media_overlay_craft_editor_commit(
        bundle,
        variant={},
        assets={
            "asset-1": {"kind": "image", "gcs_path": "users/u/plan/i/pool/1.png"},
            "visual-2": {"kind": "image", "gcs_path": "users/u/plan/i/pool/2.png"},
        },
    )
    assert commit.media_overlays is not None
    assert [card["start_s"] for card in commit.media_overlays] == [0.0, 2.0]


def test_media_overlay_command_rejects_non_finite_timing() -> None:
    with pytest.raises(ValidationError):
        _bundle(
            {
                "command": "set_media_overlay",
                "asset_id": "asset-1",
                "start_s": float("nan"),
                "end_s": 1,
            }
        )


def test_sfx_command_compiles_only_from_server_resolved_catalog_placement() -> None:
    bundle = _bundle(
        {
            "command": "set_licensed_sfx",
            "sound_effect_id": "catalog-pop",
            "at_s": 1.25,
        }
    )
    commit = build_core_craft_editor_commit(
        bundle,
        variant={"duration_s": 4.0},
        licensed_sfx={
            "id": "placement-1",
            "sound_effect_id": "catalog-pop",
            "src_gcs_path": "sound-effects/catalog-pop.wav",
            "at_s": 1.25,
        },
    )
    assert commit.sound_effects == [
        {
            "id": "placement-1",
            "sound_effect_id": "catalog-pop",
            "src_gcs_path": "sound-effects/catalog-pop.wav",
            "at_s": 1.25,
        }
    ]


def test_sfx_command_cannot_compile_without_catalog_resolution() -> None:
    bundle = _bundle(
        {
            "command": "set_licensed_sfx",
            "sound_effect_id": "catalog-pop",
            "at_s": 1.25,
        }
    )
    with pytest.raises(CreatorCraftValidationError, match="licensed sound effect"):
        build_core_craft_editor_commit(bundle, variant={"duration_s": 4.0})


def test_speech_cut_command_is_candidate_id_only_and_preview_is_bounded() -> None:
    bundle = _bundle(
        {
            "command": "apply_speech_cut",
            "candidate_id": "cut_1234567890abcdef",
            "expected_cut_revision": "rev-123",
        }
    )
    preview = craft_preview(bundle, generation="new-generation", sections={"speech_cut": True})
    assert preview["speech_cuts"] == [{"candidate_id": "cut_1234567890abcdef"}]
    assert "start_s" not in preview["speech_cuts"][0]
