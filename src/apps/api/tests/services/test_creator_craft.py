"""Core Creator craft bundle contracts and deterministic compilation."""

import uuid

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreatorCraftBundle
from app.services.creator_craft import (
    CreatorCraftValidationError,
    build_core_craft_editor_commit,
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
