"""Unit tests for ClipMetadataAgent.parse() — focuses on the empty-moments edge."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.clip_metadata import (
    ClipMetadataAgent,
    ClipMetadataInput,
    Moment,
    _enforce_moment_spread,
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


class TestEnforceMomentSpread:
    """Locks the deterministic post-filter for the TODOS.md 2026-05-13 clustering
    bug: Gemini periodically returns 3 moments inside 0.5s; matcher loses variety."""

    def test_drops_moments_within_min_spacing(self):
        # The bug fixture from TODOS.md: 3 moments inside <0.5s. The first wins;
        # the next two are dropped because their start_s is too close.
        clustered = [
            Moment(start_s=0.0, end_s=2.0, energy=8.0, description="a"),
            Moment(start_s=0.1, end_s=2.1, energy=7.5, description="b"),
            Moment(start_s=0.3, end_s=2.3, energy=7.0, description="c"),
        ]
        out = _enforce_moment_spread(clustered)
        assert len(out) == 1
        assert out[0].description == "a"

    def test_keeps_moments_spaced_at_or_above_2s(self):
        # Spacing exactly at the threshold (2.0s) should be kept.
        spaced = [
            Moment(start_s=0.0, end_s=2.0, energy=8.0, description="a"),
            Moment(start_s=2.0, end_s=4.0, energy=7.0, description="b"),
            Moment(start_s=5.0, end_s=7.0, energy=6.0, description="c"),
        ]
        out = _enforce_moment_spread(spaced)
        assert len(out) == 3

    def test_drops_sub_second_durations(self):
        # Frame-index-like windows (HARD RULE 3: ≥1s duration).
        moments = [
            Moment(start_s=0.0, end_s=0.1, energy=8.0, description="frame"),
            Moment(start_s=3.0, end_s=5.0, energy=7.0, description="real"),
        ]
        out = _enforce_moment_spread(moments)
        assert len(out) == 1
        assert out[0].description == "real"

    def test_sorts_by_start_s_before_filtering(self):
        # Out-of-order input must be sorted before greedy-accept, otherwise a
        # late-arriving early moment would reset the spacing window incorrectly.
        out_of_order = [
            Moment(start_s=5.0, end_s=7.0, energy=7.0, description="c"),
            Moment(start_s=0.0, end_s=2.0, energy=8.0, description="a"),
            Moment(start_s=2.5, end_s=4.5, energy=7.5, description="b"),
        ]
        out = _enforce_moment_spread(out_of_order)
        assert [m.description for m in out] == ["a", "b", "c"]

    def test_single_moment_returns_single_moment(self):
        # The prompt says "fewer strong moments > padding". One legit moment in,
        # one legit moment out — never invent siblings.
        single = [Moment(start_s=0.0, end_s=4.0, energy=8.0, description="only")]
        out = _enforce_moment_spread(single)
        assert len(out) == 1

    def test_empty_returns_empty(self):
        assert _enforce_moment_spread([]) == []

    def test_parse_applies_spread_filter_end_to_end(self):
        # Integration: parse() must invoke the filter so the clustering bug
        # CANNOT leak past the agent boundary into the orchestrator/matcher.
        raw = json.dumps({
            "transcript": "",
            "hook_text": "",
            "hook_score": 5.0,
            "best_moments": [
                {"start_s": 0.0, "end_s": 2.0, "energy": 8.0, "description": "a"},
                {"start_s": 0.1, "end_s": 2.1, "energy": 7.5, "description": "b"},
                {"start_s": 0.3, "end_s": 2.3, "energy": 7.0, "description": "c"},
            ],
        })
        out = _agent().parse(raw, _make_input())
        assert len(out.best_moments) == 1
        assert out.best_moments[0].description == "a"


# ── Image branch: render_prompt ──────────────────────────────────────────────────


class TestRenderPromptImageBranch:
    """render_prompt must route to the photo-specific prompt for image/* MIME types."""

    def _photo_input(self, mime: str) -> ClipMetadataInput:
        return ClipMetadataInput(file_uri="files/abc123", file_mime=mime)

    def _video_input(self) -> ClipMetadataInput:
        return ClipMetadataInput(file_uri="files/xyz", file_mime="video/mp4")

    def test_image_jpeg_routes_to_photo_prompt(self):
        agent = _agent()
        prompt = agent.render_prompt(self._photo_input("image/jpeg"))
        # Photo prompt must reference still-photography analysis, not video
        assert "photograph" in prompt.lower() or "still" in prompt.lower() or "image" in prompt.lower()

    def test_image_png_routes_to_photo_prompt(self):
        agent = _agent()
        prompt = agent.render_prompt(self._photo_input("image/png"))
        assert "photograph" in prompt.lower() or "image" in prompt.lower()

    def test_image_heic_routes_to_photo_prompt(self):
        agent = _agent()
        prompt = agent.render_prompt(self._photo_input("image/heic"))
        assert "photograph" in prompt.lower() or "image" in prompt.lower()

    def test_video_mp4_does_not_route_to_photo_prompt(self):
        agent = _agent()
        video_prompt = agent.render_prompt(self._video_input())
        photo_prompt = agent.render_prompt(self._photo_input("image/jpeg"))
        # Prompts must differ — image branch must not contaminate video path
        assert video_prompt != photo_prompt

    def test_photo_prompt_requires_text_safe_zone(self):
        """Photo prompt must request text_safe_zone (needed by _hero_composition)."""
        agent = _agent()
        prompt = agent.render_prompt(self._photo_input("image/jpeg"))
        assert "text_safe_zone" in prompt

    def test_photo_prompt_requires_visual_density(self):
        """Photo prompt must request visual_density (needed by _hero_composition)."""
        agent = _agent()
        prompt = agent.render_prompt(self._photo_input("image/jpeg"))
        assert "visual_density" in prompt
