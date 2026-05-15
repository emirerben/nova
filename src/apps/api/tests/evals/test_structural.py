"""Unit tests for runners/structural.py — pure functions, no LLM, no fixtures."""

from __future__ import annotations

import pytest

from app.agents.audio_template import AudioTemplateOutput
from app.agents.clip_metadata import ClipMetadataInput, ClipMetadataOutput, Moment, TimeRange
from app.agents.creative_direction import CreativeDirectionOutput
from app.agents.platform_copy import PlatformCopyOutput
from app.agents.template_recipe import TemplateRecipeOutput, _validate_slots, _validate_text_bbox
from app.agents.transcript import TranscriptOutput, Word
from app.pipeline.agents.copy_writer import (
    InstagramCopy,
    PlatformCopy,
    TikTokCopy,
    YouTubeCopy,
)

from .runners.structural import (
    check_audio_template,
    check_clip_metadata,
    check_creative_direction,
    check_platform_copy,
    check_template_recipe,
    check_transcript,
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
                "text_overlays": [{"role": "hook", "start_s": 0.0, "end_s": 2.0, "text": "Hi"}],
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
            dict(
                position=1,
                target_duration_s=10.0,
                slot_type="hook",
                energy=42.0,
                transition_in="hard-cut",
                color_hint="warm",
                speed_factor=1.0,
                text_overlays=[],
            ),
        ]
        out = _good_recipe(shot_count=1, total_duration_s=10.0, slots=slots, interstitials=[])
        failures = check_template_recipe(out)
        assert any("energy=42.0" in f for f in failures)

    def test_overlay_outside_slot(self):
        slots = [
            dict(
                position=1,
                target_duration_s=4.0,
                slot_type="hook",
                energy=5.0,
                transition_in="hard-cut",
                color_hint="warm",
                speed_factor=1.0,
                text_overlays=[{"role": "hook", "start_s": 5.0, "end_s": 7.0, "text": "late"}],
            ),
            dict(
                position=2,
                target_duration_s=6.0,
                slot_type="broll",
                energy=5.0,
                transition_in="dissolve",
                color_hint="warm",
                speed_factor=1.0,
                text_overlays=[],
            ),
        ]
        out = _good_recipe(slots=slots)
        failures = check_template_recipe(out)
        assert any("outside slot duration" in f for f in failures)

    def test_overlay_invalid_role(self):
        slots = [
            dict(
                position=1,
                target_duration_s=4.0,
                slot_type="hook",
                energy=5.0,
                transition_in="hard-cut",
                color_hint="warm",
                speed_factor=1.0,
                text_overlays=[{"role": "banana", "start_s": 0.0, "end_s": 1.0, "text": "x"}],
            ),
            dict(
                position=2,
                target_duration_s=6.0,
                slot_type="broll",
                energy=5.0,
                transition_in="dissolve",
                color_hint="warm",
                speed_factor=1.0,
                text_overlays=[],
            ),
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
            interstitials=[{"after_slot": 1, "type": "barn-door", "animate_s": 0.5, "hold_s": 1.0}]
        )
        failures = check_template_recipe(out)
        assert any("type='barn-door'" in f for f in failures)

    def _slot_with_bbox(self, bbox):
        return {
            "position": 1,
            "target_duration_s": 4.0,
            "slot_type": "hook",
            "energy": 8.0,
            "transition_in": "hard-cut",
            "color_hint": "warm",
            "speed_factor": 1.0,
            "text_overlays": [
                {
                    "role": "hook",
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "text": "hi",
                    "text_bbox": bbox,
                }
            ],
        }

    def test_overlay_bbox_valid_accepted(self):
        slots = [
            self._slot_with_bbox(
                {
                    "x_norm": 0.5,
                    "y_norm": 0.5,
                    "w_norm": 0.4,
                    "h_norm": 0.1,
                    "sample_frame_t": 1.0,
                }
            ),
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
        ]
        failures = check_template_recipe(_good_recipe(slots=slots))
        assert not any("text_bbox" in f for f in failures)

    def test_overlay_bbox_null_accepted(self):
        slots = [
            self._slot_with_bbox(None),
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
        ]
        failures = check_template_recipe(_good_recipe(slots=slots))
        assert not any("text_bbox" in f for f in failures)

    def test_overlay_bbox_center_out_of_range_caught(self):
        slots = [
            self._slot_with_bbox(
                {
                    "x_norm": 1.5,
                    "y_norm": 0.5,
                    "w_norm": 0.1,
                    "h_norm": 0.1,
                    "sample_frame_t": 1.0,
                }
            ),
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
        ]
        failures = check_template_recipe(_good_recipe(slots=slots))
        assert any("text_bbox center" in f for f in failures)

    def test_overlay_bbox_sample_frame_outside_window_caught(self):
        slots = [
            self._slot_with_bbox(
                {
                    "x_norm": 0.5,
                    "y_norm": 0.5,
                    "w_norm": 0.1,
                    "h_norm": 0.1,
                    "sample_frame_t": 5.0,  # window is [0, 2]
                }
            ),
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
        ]
        failures = check_template_recipe(_good_recipe(slots=slots))
        assert any("sample_frame_t" in f for f in failures)


class TestTextBboxValidation:
    """Unit tests for _validate_text_bbox — gates the new optional bbox field."""

    _OVERLAY_START = 0.0
    _OVERLAY_END = 4.0
    _VALID = {
        "x_norm": 0.5,
        "y_norm": 0.42,
        "w_norm": 0.6,
        "h_norm": 0.08,
        "sample_frame_t": 2.0,
    }

    def _v(self, raw):
        return _validate_text_bbox(raw, self._OVERLAY_START, self._OVERLAY_END)

    def test_none_returns_none(self):
        assert self._v(None) is None

    def test_missing_returns_none(self):
        # absent field == None; same path
        assert self._v(None) is None

    def test_valid_passes_through(self):
        result = self._v(dict(self._VALID))
        assert result == self._VALID

    def test_non_dict_dropped(self):
        assert self._v("not-a-bbox") is None

    def test_non_numeric_dropped(self):
        bad = {**self._VALID, "x_norm": "not-a-number"}
        assert self._v(bad) is None

    def test_x_out_of_range_high(self):
        bad = {**self._VALID, "x_norm": 1.5}
        assert self._v(bad) is None

    def test_x_out_of_range_low(self):
        bad = {**self._VALID, "x_norm": -0.1}
        assert self._v(bad) is None

    def test_zero_width_dropped(self):
        bad = {**self._VALID, "w_norm": 0.0}
        assert self._v(bad) is None

    def test_negative_height_dropped(self):
        bad = {**self._VALID, "h_norm": -0.5}
        assert self._v(bad) is None

    def test_oversize_width_dropped(self):
        bad = {**self._VALID, "w_norm": 1.2}
        assert self._v(bad) is None

    def test_horizontal_overflow_dropped(self):
        # center at 0.9, width 0.4 → extends to 1.1
        bad = {**self._VALID, "x_norm": 0.9, "w_norm": 0.4}
        assert self._v(bad) is None

    def test_vertical_overflow_dropped(self):
        # center at 0.05, height 0.2 → extends to -0.05
        bad = {**self._VALID, "y_norm": 0.05, "h_norm": 0.2}
        assert self._v(bad) is None

    def test_sample_frame_before_overlay_start(self):
        bad = {**self._VALID, "sample_frame_t": -0.5}
        assert self._v(bad) is None

    def test_sample_frame_after_overlay_end(self):
        bad = {**self._VALID, "sample_frame_t": 10.0}
        assert self._v(bad) is None

    def test_sample_frame_at_boundaries_allowed(self):
        at_start = {**self._VALID, "sample_frame_t": self._OVERLAY_START}
        at_end = {**self._VALID, "sample_frame_t": self._OVERLAY_END}
        assert self._v(at_start) is not None
        assert self._v(at_end) is not None

    def test_nan_dropped(self):
        bad = {**self._VALID, "x_norm": float("nan")}
        assert self._v(bad) is None

    def test_infinity_dropped(self):
        bad = {**self._VALID, "w_norm": float("inf")}
        assert self._v(bad) is None


class TestSlotBboxIntegration:
    """End-to-end: bbox passes through _validate_slots without breaking overlays.

    Locks in PR1's contract: overlay survives, bbox is normalized to dict-or-None,
    and the `text_bbox` key is always present after parsing (even when absent in
    the raw input) so downstream consumers can rely on its shape.
    """

    def _slot_with_overlay_bbox(self, bbox):
        overlay = {
            "role": "hook",
            "start_s": 0.0,
            "end_s": 2.0,
            "sample_text": "hi",
        }
        if bbox is not _ABSENT:
            overlay["text_bbox"] = bbox
        return {
            "position": 1,
            "target_duration_s": 4.0,
            "slot_type": "hook",
            "text_overlays": [overlay],
        }

    def test_valid_bbox_preserved(self):
        slot = self._slot_with_overlay_bbox(
            {
                "x_norm": 0.5,
                "y_norm": 0.5,
                "w_norm": 0.4,
                "h_norm": 0.1,
                "sample_frame_t": 1.0,
            }
        )
        _validate_slots([slot], global_color_grade="none")
        ov = slot["text_overlays"][0]
        assert ov["text_bbox"] == {
            "x_norm": 0.5,
            "y_norm": 0.5,
            "w_norm": 0.4,
            "h_norm": 0.1,
            "sample_frame_t": 1.0,
        }

    def test_bad_bbox_dropped_but_overlay_survives(self):
        slot = self._slot_with_overlay_bbox(
            {
                "x_norm": 2.0,  # out of range
                "y_norm": 0.5,
                "w_norm": 0.4,
                "h_norm": 0.1,
                "sample_frame_t": 1.0,
            }
        )
        _validate_slots([slot], global_color_grade="none")
        overlays = slot["text_overlays"]
        assert len(overlays) == 1, "overlay must survive even if bbox is invalid"
        assert overlays[0]["text_bbox"] is None
        # Other overlay fields untouched
        assert overlays[0]["role"] == "hook"
        assert overlays[0]["sample_text"] == "hi"

    def test_absent_bbox_becomes_none(self):
        slot = self._slot_with_overlay_bbox(_ABSENT)
        _validate_slots([slot], global_color_grade="none")
        ov = slot["text_overlays"][0]
        # PR1 contract: text_bbox key is always present after parsing
        assert "text_bbox" in ov
        assert ov["text_bbox"] is None

    def test_explicit_null_bbox_stays_none(self):
        slot = self._slot_with_overlay_bbox(None)
        _validate_slots([slot], global_color_grade="none")
        assert slot["text_overlays"][0]["text_bbox"] is None

    def test_sample_frame_outside_window_dropped(self):
        slot = self._slot_with_overlay_bbox(
            {
                "x_norm": 0.5,
                "y_norm": 0.5,
                "w_norm": 0.4,
                "h_norm": 0.1,
                "sample_frame_t": 99.0,  # overlay window is [0, 2]
            }
        )
        _validate_slots([slot], global_color_grade="none")
        assert slot["text_overlays"][0]["text_bbox"] is None


_ABSENT = object()  # sentinel: distinguishes "key missing" from "key=None"


def _good_clip_metadata(**overrides) -> ClipMetadataOutput:
    base = dict(
        clip_id="",
        transcript="hello world",
        hook_text="That's an incredible save!",
        hook_score=8.0,
        best_moments=[
            Moment(
                start_s=0.0,
                end_s=3.5,
                energy=9.0,
                description="goalkeeper diving save attempt",
            ),
            Moment(
                start_s=4.0,
                end_s=8.0,
                energy=7.0,
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


def _good_transcript(**overrides) -> TranscriptOutput:
    base = dict(
        words=[
            Word(text="Hello", start_s=0.0, end_s=0.5, confidence=0.95),
            Word(text="world", start_s=0.6, end_s=1.0, confidence=0.92),
            Word(text="this", start_s=1.1, end_s=1.4, confidence=0.9),
            Word(text="is", start_s=1.5, end_s=1.7, confidence=0.9),
            Word(text="fine", start_s=1.8, end_s=2.2, confidence=0.95),
        ],
        full_text="Hello world this is fine",
        low_confidence=False,
    )
    base.update(overrides)
    return TranscriptOutput(**base)


class TestTranscriptStructural:
    def test_valid(self):
        assert check_transcript(_good_transcript(), None) == []

    def test_empty_words(self):
        out = _good_transcript(words=[], full_text="")
        failures = check_transcript(out, None)
        assert any("words list is empty" in f for f in failures)

    def test_overlapping_words(self):
        out = _good_transcript(
            words=[
                Word(text="Hello", start_s=0.0, end_s=1.0, confidence=0.95),
                Word(text="world", start_s=0.5, end_s=1.2, confidence=0.95),
            ],
            full_text="Hello world",
        )
        failures = check_transcript(out, None)
        assert any("overlaps prior" in f for f in failures)

    def test_full_text_misaligned(self):
        out = _good_transcript(full_text="completely different content here")
        failures = check_transcript(out, None)
        assert any("does not align" in f for f in failures)

    def test_low_confidence_false_with_low_avg(self):
        out = _good_transcript(
            words=[
                Word(text="Hello", start_s=0.0, end_s=0.5, confidence=0.3),
                Word(text="world", start_s=0.6, end_s=1.0, confidence=0.3),
            ],
            full_text="Hello world",
            low_confidence=False,
        )
        failures = check_transcript(out, None)
        assert any("avg word confidence" in f for f in failures)


def _good_platform_copy(**overrides) -> PlatformCopyOutput:
    base = dict(
        tiktok=TikTokCopy(
            hook="this save shouldn't be possible",
            caption="keeper came out of nowhere on this one",
            hashtags=["#football", "#save", "#fyp", "#sports", "#highlights"],
        ),
        instagram=InstagramCopy(
            hook="incredible last-second save",
            caption="goalkeeper read the play and got across",
            hashtags=["#football", "#goalkeeper", "#save", "#soccer", "#sports"],
        ),
        youtube=YouTubeCopy(
            title="Insane goalkeeper save #shorts",
            description="Goalkeeper makes an unbelievable last-second save.",
            tags=["football", "goalkeeper", "save", "shorts"],
        ),
    )
    base.update(overrides)
    pc = PlatformCopy(**base)
    return PlatformCopyOutput(value=pc)


class TestPlatformCopyStructural:
    def test_valid(self):
        assert check_platform_copy(_good_platform_copy()) == []

    def test_empty_tiktok_hook(self):
        out = _good_platform_copy(tiktok=TikTokCopy(hook="", caption="x", hashtags=["#a"]))
        failures = check_platform_copy(out)
        assert any("tiktok.hook" in f and "empty" in f for f in failures)

    def test_placeholder_leaks(self):
        out = _good_platform_copy(
            tiktok=TikTokCopy(
                hook="watch {subject} now",
                caption="caption text",
                hashtags=["#a"],
            )
        )
        failures = check_platform_copy(out)
        assert any("placeholder" in f for f in failures)

    def test_too_few_instagram_hashtags(self):
        out = _good_platform_copy(
            instagram=InstagramCopy(hook="hook", caption="caption", hashtags=["#a", "#b"])
        )
        failures = check_platform_copy(out)
        assert any("instagram.hashtags" in f and "≥ 3" in f for f in failures)

    def test_duplicate_hooks_caught(self):
        out = _good_platform_copy(
            tiktok=TikTokCopy(hook="exact same hook text", caption="x", hashtags=["#a"]),
            instagram=InstagramCopy(
                hook="exact same hook text",
                caption="y",
                hashtags=["#a", "#b", "#c"],
            ),
        )
        failures = check_platform_copy(out)
        assert any("identical" in f for f in failures)


def _good_audio_template(**overrides) -> AudioTemplateOutput:
    base = dict(
        shot_count=2,
        total_duration_s=8.0,
        hook_duration_s=2.0,
        slots=[
            {
                "position": 1,
                "target_duration_s": 4.0,
                "slot_type": "hook",
                "energy": 8.0,
                "transition_in": "hard-cut",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "text_overlays": [],
            },
            {
                "position": 2,
                "target_duration_s": 4.0,
                "slot_type": "broll",
                "energy": 6.0,
                "transition_in": "dissolve",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "text_overlays": [],
            },
        ],
        copy_tone="upbeat",
        caption_style="bold sans",
        beat_timestamps_s=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
        creative_direction="Driving track with snappy cuts on every beat.",
        transition_style="hard cuts on the beat",
        color_grade="warm",
        pacing_style="snappy 0.5s cuts",
        sync_style="cut-on-beat",
        interstitials=[],
        subject_niche="music montage",
        has_talking_head=False,
        has_voiceover=False,
        has_permanent_letterbox=False,
    )
    base.update(overrides)
    return AudioTemplateOutput(**base)


class TestAudioTemplateStructural:
    def test_valid(self):
        assert check_audio_template(_good_audio_template()) == []

    def test_shot_count_mismatch(self):
        out = _good_audio_template(shot_count=5)
        failures = check_audio_template(out)
        assert any("shot_count=5 != len(slots)=2" in f for f in failures)

    def test_slot_duration_mismatch(self):
        out = _good_audio_template(total_duration_s=30.0)
        failures = check_audio_template(out)
        assert any("slot durations sum" in f for f in failures)

    def test_unsorted_beats(self):
        out = _good_audio_template(beat_timestamps_s=[1.0, 0.5, 1.5])
        failures = check_audio_template(out)
        assert any("not sorted" in f for f in failures)

    def test_empty_creative_direction(self):
        out = _good_audio_template(creative_direction="")
        failures = check_audio_template(out)
        assert any("creative_direction" in f and "empty" in f for f in failures)

    def test_invalid_color_grade(self):
        out = _good_audio_template(color_grade="rainbow")
        failures = check_audio_template(out)
        assert any("color_grade='rainbow'" in f for f in failures)


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
        assert (
            run_structural(
                "nova.compose.creative_direction", CreativeDirectionOutput(text=text), None
            )
            == []
        )

    def test_dispatches_transcript(self):
        assert run_structural("nova.audio.transcript", _good_transcript(), None) == []

    def test_dispatches_platform_copy(self):
        assert run_structural("nova.compose.platform_copy", _good_platform_copy(), None) == []

    def test_dispatches_audio_template(self):
        assert run_structural("nova.audio.template_recipe", _good_audio_template(), None) == []

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError):
            run_structural("nope.unknown", None, None)
