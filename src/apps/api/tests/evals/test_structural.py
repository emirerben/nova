"""Unit tests for runners/structural.py — pure functions, no LLM, no fixtures."""

from __future__ import annotations

import pytest

from app.agents.clip_metadata import ClipMetadataInput, ClipMetadataOutput, Moment, TimeRange
from app.agents.creative_direction import CreativeDirectionOutput
from app.agents.template_recipe import TemplateRecipeOutput

from .runners.structural import (
    check_clip_metadata,
    check_creative_direction,
    check_template_recipe,
    run_structural,
)


def _good_recipe(**overrides) -> TemplateRecipeOutput:
    base = dict(
        shot_count=2,
        total_duration_s=10.0,
        hook_duration_s=3.0,
        slots=[
            {
                "position": 1,
                "target_duration_s": 4.0,
                "slot_type": "hook",
                "energy": 8.0,
                "transition_in": "hard-cut",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "text_overlays": [
                    {"role": "hook", "start_s": 0.0, "end_s": 2.0, "text": "Hi"}
                ],
            },
            {
                "position": 2,
                "target_duration_s": 6.0,
                "slot_type": "broll",
                "energy": 5.0,
                "transition_in": "dissolve",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "text_overlays": [],
            },
        ],
        copy_tone="energetic",
        caption_style="bold",
        beat_timestamps_s=[],
        creative_direction="",
        transition_style="cut-on-beat",
        color_grade="warm",
        pacing_style="snappy",
        sync_style="cut-on-beat",
        interstitials=[
            {
                "after_slot": 1,
                "type": "curtain-close",
                "animate_s": 0.5,
                "hold_s": 1.0,
                "hold_color": "#000000",
            }
        ],
        subject_niche="lifestyle",
        has_talking_head=False,
        has_voiceover=False,
        has_permanent_letterbox=False,
    )
    base.update(overrides)
    return TemplateRecipeOutput(**base)


class TestTemplateRecipeStructural:
    def test_valid_recipe_no_failures(self):
        assert check_template_recipe(_good_recipe()) == []

    def test_shot_count_mismatch(self):
        out = _good_recipe(shot_count=5)  # 5 != 2 actual slots
        failures = check_template_recipe(out)
        assert any("shot_count" in f for f in failures)

    def test_slot_duration_mismatch_over_5s(self):
        out = _good_recipe(total_duration_s=30.0)  # slots sum to 10, not 30
        failures = check_template_recipe(out)
        assert any("slot durations sum" in f for f in failures)

    def test_slot_duration_mismatch_within_tolerance(self):
        out = _good_recipe(total_duration_s=14.0)  # slots sum to 10; delta 4.0 < 5.0
        failures = check_template_recipe(out)
        assert not any("slot durations sum" in f for f in failures)

    def test_energy_out_of_range(self):
        slots = [
            dict(position=1, target_duration_s=10.0, slot_type="hook", energy=42.0,
                 transition_in="hard-cut", color_hint="warm", speed_factor=1.0,
                 text_overlays=[]),
        ]
        out = _good_recipe(shot_count=1, total_duration_s=10.0, slots=slots, interstitials=[])
        failures = check_template_recipe(out)
        assert any("energy=42.0" in f for f in failures)

    def test_overlay_outside_slot(self):
        slots = [
            dict(position=1, target_duration_s=4.0, slot_type="hook", energy=5.0,
                 transition_in="hard-cut", color_hint="warm", speed_factor=1.0,
                 text_overlays=[
                     {"role": "hook", "start_s": 5.0, "end_s": 7.0, "text": "late"}
                 ]),
            dict(position=2, target_duration_s=6.0, slot_type="broll", energy=5.0,
                 transition_in="dissolve", color_hint="warm", speed_factor=1.0,
                 text_overlays=[]),
        ]
        out = _good_recipe(slots=slots)
        failures = check_template_recipe(out)
        assert any("outside slot duration" in f for f in failures)

    def test_overlay_invalid_role(self):
        slots = [
            dict(position=1, target_duration_s=4.0, slot_type="hook", energy=5.0,
                 transition_in="hard-cut", color_hint="warm", speed_factor=1.0,
                 text_overlays=[
                     {"role": "banana", "start_s": 0.0, "end_s": 1.0, "text": "x"}
                 ]),
            dict(position=2, target_duration_s=6.0, slot_type="broll", energy=5.0,
                 transition_in="dissolve", color_hint="warm", speed_factor=1.0,
                 text_overlays=[]),
        ]
        out = _good_recipe(slots=slots)
        failures = check_template_recipe(out)
        assert any("role='banana'" in f for f in failures)

    def test_interstitial_after_slot_out_of_range(self):
        out = _good_recipe(
            interstitials=[
                {"after_slot": 99, "type": "curtain-close", "animate_s": 0.5, "hold_s": 1.0}
            ]
        )
        failures = check_template_recipe(out)
        assert any("after_slot=99" in f for f in failures)

    def test_interstitial_invalid_type(self):
        out = _good_recipe(
            interstitials=[
                {"after_slot": 1, "type": "barn-door", "animate_s": 0.5, "hold_s": 1.0}
            ]
        )
        failures = check_template_recipe(out)
        assert any("type='barn-door'" in f for f in failures)


def _good_clip_metadata(**overrides) -> ClipMetadataOutput:
    base = dict(
        clip_id="",
        transcript="hello world",
        hook_text="That's an incredible save!",
        hook_score=8.0,
        best_moments=[
            Moment(
                start_s=0.0, end_s=3.5, energy=9.0,
                description="goalkeeper diving save attempt",
            ),
            Moment(
                start_s=4.0, end_s=8.0, energy=7.0,
                description="counter-attack pass forward",
            ),
        ],
        detected_subject="football",
    )
    base.update(overrides)
    return ClipMetadataOutput(**base)


def _input(filter_hint: str = "", segment: TimeRange | None = None) -> ClipMetadataInput:
    return ClipMetadataInput(
        file_uri="files/abc",
        file_mime="video/mp4",
        segment=segment,
        filter_hint=filter_hint,
    )


class TestClipMetadataStructural:
    def test_valid_no_failures(self):
        assert check_clip_metadata(_good_clip_metadata(), _input()) == []

    def test_hook_score_out_of_range_caught_by_pydantic(self):
        with pytest.raises(Exception):
            _good_clip_metadata(hook_score=42.0)

    def test_too_few_moments(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=0.0, end_s=3.5, energy=9.0, description="goalkeeper save attempt")
            ]
        )
        failures = check_clip_metadata(out, _input())
        assert any("2-5" in f for f in failures)

    def test_too_many_moments(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=i, end_s=i + 1.0, energy=5.0, description="pass through midfield")
                for i in range(6)
            ]
        )
        failures = check_clip_metadata(out, _input())
        assert any("6 entries" in f for f in failures)

    def test_vague_description_blocked(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=0.0, end_s=3.0, energy=8.0, description="player on field"),
                Moment(start_s=4.0, end_s=8.0, energy=7.0, description="counter-attack pass"),
            ]
        )
        failures = check_clip_metadata(out, _input())
        assert any("vague-label blocklist" in f for f in failures)

    def test_short_description_caught(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=0.0, end_s=3.0, energy=8.0, description="goal!"),
                Moment(start_s=4.0, end_s=8.0, energy=7.0, description="counter-attack pass"),
            ]
        )
        failures = check_clip_metadata(out, _input())
        assert any("< 3 words" in f for f in failures)

    def test_football_filter_kept_celebration(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(
                    start_s=0.0,
                    end_s=3.0,
                    energy=8.0,
                    description="goalkeeper celebration after save",
                ),
                Moment(start_s=4.0, end_s=8.0, energy=7.0, description="counter-attack pass"),
            ]
        )
        failures = check_clip_metadata(out, _input(filter_hint="ball must be in frame"))
        assert any("blacklisted" in f for f in failures)

    def test_football_filter_no_whitelist_verb(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=0.0, end_s=3.0, energy=8.0, description="player runs forward fast"),
                Moment(start_s=4.0, end_s=8.0, energy=7.0, description="midfielder turns around"),
            ]
        )
        failures = check_clip_metadata(out, _input(filter_hint="ball must be in frame"))
        assert any("whitelist verb" in f for f in failures)

    def test_long_clip_needs_long_moment(self):
        out = _good_clip_metadata(
            best_moments=[
                Moment(start_s=0.0, end_s=3.0, energy=8.0, description="opening pass forward"),
                Moment(start_s=10.0, end_s=14.0, energy=7.0, description="counter-attack run"),
            ]
        )
        seg = TimeRange(start_s=0.0, end_s=40.0)
        failures = check_clip_metadata(out, _input(segment=seg))
        assert any(">= 13s" in f or "≥ 13s" in f for f in failures)


class TestCreativeDirectionStructural:
    def test_valid(self):
        text = (
            "The pacing is snappy with quick whip-pan transitions on the beat drops. "
            "Color grading leans warm and high-contrast with mild orange-teal feel. "
            "Speed ramps slow down on action moments before snapping back to full speed. "
            "Audio is sync'd hard to the beat with a music-led drop. "
            "There is no on-camera host or voiceover narration; pure visual storytelling. "
            "No letterbox bars; full bleed 9:16 framing. Niche is sports highlight reels."
        )
        out = CreativeDirectionOutput(text=text)
        assert check_creative_direction(out) == []

    def test_too_short(self):
        out = CreativeDirectionOutput(text="Short.")
        failures = check_creative_direction(out)
        assert any("below floor of 50" in f for f in failures)

    def test_too_long(self):
        out = CreativeDirectionOutput(text="word " * 500)
        failures = check_creative_direction(out)
        assert any("above ceiling of 400" in f for f in failures)

    def test_too_few_topics(self):
        out = CreativeDirectionOutput(
            text=(
                "It is a video. It has nice shots. The vibe is fun and energetic. "
                "Some scenes are fast and some are slow but it doesn't mention "
                "any specific elements. It just describes vibes and feelings only."
            )
        )
        failures = check_creative_direction(out)
        assert any("topics mentioned" in f for f in failures)


class TestRunStructuralDispatch:
    def test_dispatches_template_recipe(self):
        assert run_structural("nova.compose.template_recipe", _good_recipe(), None) == []

    def test_dispatches_clip_metadata(self):
        assert run_structural("nova.video.clip_metadata", _good_clip_metadata(), _input()) == []

    def test_dispatches_creative_direction(self):
        text = (
            "The pacing is snappy with quick whip-pan transitions on the beat drops. "
            "Color grading leans warm and high-contrast for maximum punch. "
            "Speed ramps slow down on key action moments before snapping back to "
            "full speed. Audio sync is locked tightly to the beat with a music "
            "drop. There is no on-camera host or voiceover narration. No letterbox "
            "bars; full bleed framing. The niche is sports highlight reels."
        )
        assert run_structural(
            "nova.compose.creative_direction", CreativeDirectionOutput(text=text), None
        ) == []

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError):
            run_structural("nope.unknown", None, None)
