"""Unit tests for ClipMetadataAgent.parse() — focuses on the empty-moments edge."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.clip_metadata import (
    ClipMetadataAgent,
    ClipMetadataInput,
)


def _make_input() -> ClipMetadataInput:
    return ClipMetadataInput(
        file_uri="files/abc",
        file_mime="video/mp4",
        segment=None,
        filter_hint="",
    )


def _agent() -> ClipMetadataAgent:
    # parse() doesn't touch the model client, so any sentinel works.
    return ClipMetadataAgent(model_client=None)  # type: ignore[arg-type]


class TestClipMetadataParse:
    def test_parse_happy_path(self):
        raw = json.dumps({
            "transcript": "hello world",
            "hook_text": "wait for it",
            "hook_score": 8.5,
            "best_moments": [
                {"start_s": 0.0, "end_s": 5.0, "energy": 7.0, "description": "intro"},
                {"start_s": 5.0, "end_s": 10.0, "energy": 8.0, "description": "twist"},
            ],
            "detected_subject": "kitchen scene",
        })

        out = _agent().parse(raw, _make_input())

        assert len(out.best_moments) == 2
        assert out.hook_score == pytest.approx(8.5)
        assert out.detected_subject == "kitchen scene"

    def test_parse_truly_empty_moments_passes_through(self):
        """Gemini explicitly returned an empty list (no usable moments). Parse
        accepts that — refusal is the runtime's job (best_moments is in
        required_fields). Parse here returns what it got."""
        raw = json.dumps({
            "transcript": "",
            "hook_text": "headline",
            "hook_score": 5.0,
            "best_moments": [],
        })

        out = _agent().parse(raw, _make_input())

        assert out.best_moments == []

    def test_parse_raises_when_every_moment_fails_coercion(self):
        """Repro for the prod failure: Gemini returned moments with wrong
        field names (e.g. `start`/`end` instead of `start_s`/`end_s`).
        Each entry fails Moment validation in the silent-drop loop. Without
        the new guard, parse returned an empty list as success and the
        Whisper fallback never engaged. With the guard, parse raises
        SchemaError so the runtime retries with clarification."""
        raw = json.dumps({
            "transcript": "ok",
            "hook_text": "headline",
            "hook_score": 5.0,
            # Note: "start"/"end" instead of "start_s"/"end_s" — every coerced
            # Moment ends up with start_s=0, end_s=0 which is technically valid
            # for Moment (both fields default to 0.0 with ge=0). To force the
            # drop we use values that fail ge=0 validation.
            "best_moments": [
                {"start_s": -1.0, "end_s": 5.0, "description": "bad"},
                {"start_s": -2.0, "end_s": 10.0, "description": "bad"},
            ],
        })

        with pytest.raises(SchemaError) as exc_info:
            _agent().parse(raw, _make_input())

        assert "every entry failed validation" in str(exc_info.value)

    def test_parse_keeps_partial_when_some_moments_valid(self):
        """If some moments validate and others don't, keep the valid ones —
        better to have partial data than fail the whole analysis."""
        raw = json.dumps({
            "transcript": "ok",
            "hook_text": "headline",
            "hook_score": 5.0,
            "best_moments": [
                {"start_s": 0.0, "end_s": 5.0, "description": "good"},
                {"start_s": -1.0, "end_s": 5.0, "description": "bad — negative"},
                {"start_s": 5.0, "end_s": 10.0, "description": "good"},
            ],
        })

        out = _agent().parse(raw, _make_input())

        # The two valid ones survive; the negative-start one is silently dropped.
        # Guard does NOT fire because we kept some.
        assert len(out.best_moments) == 2
