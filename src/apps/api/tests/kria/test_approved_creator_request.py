"""KRI-524 approval pins full creator instructions with the brief contract."""

from __future__ import annotations

import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="title",
                kind="text",
                scope="title",
                literal="Weekend",
                description="typewriter title",
                facts={"animation": "typewriter"},
            )
        ],
    )


def test_binding_keeps_original_instruction_when_followup_is_only_choice() -> None:
    original = "Split the title text into words, then animate each word in sequence."
    binding = BriefBinding.create(
        uuid.uuid4(), _brief(), latest_message="1", full_creator_request=f"{original}\n1"
    )
    assert original in binding.creator_request
    assert "Latest message: 1" in binding.creator_request
    assert "typewriter" in binding.creator_request


def test_binding_excludes_messages_after_approved_source_turn() -> None:
    # The runtime helper supplies a source-event-bounded raw request; this pins
    # exactly that immutable value without consulting later thread history.
    binding = BriefBinding.create(
        uuid.uuid4(), _brief(), latest_message="1", full_creator_request="initial request\n1"
    )
    assert "later correction" not in binding.creator_request


def test_binding_rejects_combined_raw_and_brief_overflow() -> None:
    with pytest.raises(ValueError, match="safe limit"):
        BriefBinding.create(
            uuid.uuid4(),
            _brief(),
            full_creator_request="x" * 12_000,
        )


def test_legacy_binding_digest_remains_valid_without_full_request() -> None:
    binding = BriefBinding.create(uuid.uuid4(), _brief(), latest_message="Keep it short")
    assert BriefBinding.model_validate(binding.model_dump(mode="json")) == binding
