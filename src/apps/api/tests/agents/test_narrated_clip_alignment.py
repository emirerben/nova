"""Strict parse contract for the narrated clip-alignment agent (KRI-456)."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.narrated_clip_alignment import (
    NarratedClipAlignmentAgent,
    NarratedClipAlignmentInput,
)


def _input(*, order_locked: bool = True) -> NarratedClipAlignmentInput:
    return NarratedClipAlignmentInput(
        words=[
            {"word_id": f"w{i:06d}", "text": text, "start_s": float(i), "end_s": i + 0.9}
            for i, text in enumerate(["Boil", "the", "pasta.", "Now", "grate", "cheese.", "Eat."])
        ],
        clips=[
            {"clip_id": "a", "description": "pasta in boiling water", "creator_label": "boiling"},
            {"clip_id": "b", "description": "hand grating cheese"},
            {"clip_id": "c", "description": "person eating"},
        ],
        creator_request="eating at the very end",
        order_locked=order_locked,
        language="en",
    )


def _raw(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {
            "placements": [
                {"clip_id": clip_id, "start_word_id": word_id, "reason": "r"}
                for clip_id, word_id in pairs
            ]
        }
    )


def _parse(raw: str, **kwargs):
    return NarratedClipAlignmentAgent(None).parse(raw, _input(**kwargs))


def test_valid_placements_round_trip() -> None:
    output = _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000006")))

    assert [(p.clip_id, p.start_word_id) for p in output.placements] == [
        ("a", "w000000"),
        ("b", "w000003"),
        ("c", "w000006"),
    ]
    assert output.placements[0].reason == "r"


def test_first_clip_may_start_after_the_first_word() -> None:
    # The worker pins the first step to 0.0 so the intro stays with the first clip.
    output = _parse(_raw(("a", "w000001"), ("b", "w000003"), ("c", "w000006")))
    assert output.placements[0].start_word_id == "w000001"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"placements": "nope"}),
        json.dumps({"placements": [1, 2, 3]}),
    ],
)
def test_malformed_payloads_are_rejected(raw: str) -> None:
    with pytest.raises(SchemaError):
        _parse(raw)


def test_unknown_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="unknown clip"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("invented", "w000006")))


def test_unknown_word_is_rejected() -> None:
    with pytest.raises(SchemaError, match="unknown start word"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000099")))


def test_duplicate_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="duplicate clip"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("b", "w000006")))


def test_missing_clip_is_rejected() -> None:
    with pytest.raises(SchemaError, match="expected 3 placements"):
        _parse(_raw(("a", "w000000"), ("b", "w000003")))


def test_non_increasing_words_are_rejected() -> None:
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000000"), ("b", "w000003"), ("c", "w000003")))
    with pytest.raises(SchemaError, match="strictly increasing"):
        _parse(_raw(("a", "w000000"), ("b", "w000005"), ("c", "w000002")))


def test_reorder_is_rejected_when_order_is_locked() -> None:
    reordered = _raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006"))
    with pytest.raises(SchemaError, match="order is locked"):
        _parse(reordered, order_locked=True)


def test_reorder_is_accepted_when_order_is_free() -> None:
    output = _parse(_raw(("b", "w000000"), ("a", "w000003"), ("c", "w000006")), order_locked=False)
    assert [p.clip_id for p in output.placements] == ["b", "a", "c"]


def test_prompt_states_the_order_mode_and_carries_labels() -> None:
    agent = NarratedClipAlignmentAgent(None)
    locked = agent.render_prompt(_input(order_locked=True))
    free = agent.render_prompt(_input(order_locked=False))

    assert "CLIP ORDER: LOCKED" in locked
    assert "CLIP ORDER: FREE" in free
    assert '"creator_label": "boiling"' in locked
    assert "w000006" in locked
