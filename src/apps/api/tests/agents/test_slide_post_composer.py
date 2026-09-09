"""Guards for slide_post_composer's parse-time safety contract.

Focused on the id-coverage enforcement: the agent may only reorder/select
from the ids it was given — a dropped, invented, or duplicated id must be a
hard SchemaError (triggers the clarification retry, then the caller's
fallback to pool order), never a silent partial post.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.slide_post_composer import (
    SlidePostComposerAgent,
    SlidePostComposerInput,
    SlidePostMediaItem,
)


def _agent() -> SlidePostComposerAgent:
    return SlidePostComposerAgent(None)  # parse() never touches self.client


def _input(ids: list[str]) -> SlidePostComposerInput:
    return SlidePostComposerInput(media=[SlidePostMediaItem(id=i, kind="image") for i in ids])


class TestParseHappyPath:
    def test_valid_order_and_cover_round_trip(self):
        raw = json.dumps({"order": ["b", "a"], "cover_id": "a", "caption": "hi"})
        output = _agent().parse(raw, _input(["a", "b"]))
        assert output.order == ["b", "a"]
        assert output.cover_id == "a"
        assert output.caption == "hi"

    def test_alt_text_filtered_to_known_ids_only(self):
        raw = json.dumps(
            {
                "order": ["a", "b"],
                "cover_id": "a",
                "alt_text": {"a": "a photo", "unknown-id": "should be dropped"},
            }
        )
        output = _agent().parse(raw, _input(["a", "b"]))
        assert output.alt_text == {"a": "a photo"}

    def test_cover_id_not_in_known_ids_falls_back_to_first_ordered(self):
        raw = json.dumps({"order": ["b", "a"], "cover_id": "not-a-real-id"})
        output = _agent().parse(raw, _input(["a", "b"]))
        assert output.cover_id == "b"

    def test_caption_truncated_to_cap(self):
        raw = json.dumps({"order": ["a"], "cover_id": "a", "caption": "x" * 3000})
        output = _agent().parse(raw, _input(["a"]))
        assert len(output.caption) == 2200


class TestParseRejectsBrokenIdCoverage:
    def test_dropped_id_raises_schema_error(self):
        raw = json.dumps({"order": ["a"], "cover_id": "a"})
        with pytest.raises(SchemaError):
            _agent().parse(raw, _input(["a", "b"]))

    def test_invented_id_raises_schema_error(self):
        raw = json.dumps({"order": ["a", "b", "invented"], "cover_id": "a"})
        with pytest.raises(SchemaError):
            _agent().parse(raw, _input(["a", "b"]))

    def test_duplicated_id_raises_schema_error(self):
        raw = json.dumps({"order": ["a", "a"], "cover_id": "a"})
        with pytest.raises(SchemaError):
            _agent().parse(raw, _input(["a", "b"]))

    def test_invalid_json_raises_schema_error(self):
        with pytest.raises(SchemaError):
            _agent().parse("not json", _input(["a"]))

    def test_non_object_json_raises_schema_error(self):
        with pytest.raises(SchemaError):
            _agent().parse("[1, 2, 3]", _input(["a"]))
