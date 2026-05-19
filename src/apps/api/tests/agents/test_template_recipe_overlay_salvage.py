"""Overlay timing salvage in TemplateRecipeAgent._validate_slots.

Background: Gemini occasionally returns text overlay timings with
(start, end) inverted. Observed in prod on template fdaf3bbc-… where the
opening "It's" reveal came back as start=3.0, end=0.88 and was silently
dropped — leaving the rendered output missing its first word.

The agent now attempts to swap the pair when both values are non-negative
and start sits inside the slot window. Anything weirder still gets dropped.
"""

from __future__ import annotations

import json

from app.agents.template_recipe import (
    BlackSegment,
    TemplateRecipeAgent,
    TemplateRecipeInput,
    _validate_slots,
)


def _agent() -> TemplateRecipeAgent:
    return TemplateRecipeAgent(model_client=None)  # type: ignore[arg-type]


def _input(*, black_segments: list[BlackSegment] | None = None) -> TemplateRecipeInput:
    return TemplateRecipeInput(
        file_uri="files/abc",
        file_mime="video/mp4",
        analysis_mode="single",
        creative_direction="",
        black_segments=black_segments or [],
    )


def _recipe_with_overlay(start_s: float, end_s: float, slot_dur: float = 10.08) -> dict:
    return {
        "shot_count": 1,
        "total_duration_s": slot_dur,
        "hook_duration_s": slot_dur,
        "creative_direction": "",
        "copy_tone": "casual",
        "caption_style": "",
        "slots": [
            {
                "position": 1,
                "target_duration_s": slot_dur,
                "slot_type": "hook",
                "energy": 5.0,
                "text_overlays": [
                    {
                        "role": "hook",
                        "start_s": start_s,
                        "end_s": end_s,
                        "position": "center",
                        "font_size_hint": "large",
                        "text": "It's",
                        "sample_text": "It's",
                    }
                ],
            }
        ],
        "interstitials": [],
    }


class TestOverlayTimingSalvage:
    def test_inverted_timing_in_slot_is_swapped(self):
        """start=3.0, end=0.88 — both inside the 10.08s slot. Salvage swap."""
        payload = _recipe_with_overlay(start_s=3.0, end_s=0.88, slot_dur=10.08)
        out = _agent().parse(json.dumps(payload), _input())
        overlays = out.slots[0]["text_overlays"]
        assert len(overlays) == 1, "salvaged overlay should be preserved, not dropped"
        assert overlays[0]["start_s"] == 0.88
        assert overlays[0]["end_s"] == 3.0
        assert overlays[0]["sample_text"] == "It's"

    def test_inverted_timing_with_negative_end_is_dropped(self):
        """start=3.0, end=-1.0 — not safe to swap (end was nonsensical). Drop."""
        payload = _recipe_with_overlay(start_s=3.0, end_s=-1.0, slot_dur=10.08)
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["text_overlays"] == []

    def test_inverted_timing_with_start_outside_slot_is_dropped(self):
        """start=20.0, end=2.0 in a 10s slot — swapping would put end past slot. Drop."""
        payload = _recipe_with_overlay(start_s=20.0, end_s=2.0, slot_dur=10.0)
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["text_overlays"] == []

    def test_equal_timing_is_dropped(self):
        """start == end is a zero-duration overlay, not a swap case. Drop."""
        payload = _recipe_with_overlay(start_s=2.0, end_s=2.0, slot_dur=10.0)
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["text_overlays"] == []

    def test_valid_timing_is_unchanged(self):
        """start < end and both in slot — pass through untouched."""
        payload = _recipe_with_overlay(start_s=1.0, end_s=3.0, slot_dur=10.0)
        out = _agent().parse(json.dumps(payload), _input())
        overlays = out.slots[0]["text_overlays"]
        assert len(overlays) == 1
        assert overlays[0]["start_s"] == 1.0
        assert overlays[0]["end_s"] == 3.0

    def test_prod_regression_its_overlay(self):
        """Regression fixture: the actual 'It's' overlay from template
        fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102's cached recipe in prod
        (2026-05-16) — start=3.0, end=0.88, slot_dur=10.08. Must salvage."""
        payload = _recipe_with_overlay(start_s=3.0, end_s=0.88, slot_dur=10.08)
        out = _agent().parse(json.dumps(payload), _input())
        overlays = out.slots[0]["text_overlays"]
        assert len(overlays) == 1
        # After salvage, both values are well-ordered and inside the slot.
        assert 0.0 <= overlays[0]["start_s"] < overlays[0]["end_s"] <= 10.08

    def test_validate_slots_drops_inverted_when_slot_dur_zero(self):
        """Defense-in-depth: _validate_slots can be called on cached/legacy
        recipes via the audio_template path. With slot_dur=0 the salvage
        gate stays closed, and the overlay falls through to the standard
        start>=end drop. (parse() raises RefusalError on slot_dur<=0, so
        this path is only reachable when _validate_slots is called directly.)
        """
        slots = [
            {
                "position": 1,
                "target_duration_s": 0.0,  # zero — disables salvage
                "text_overlays": [
                    {
                        "role": "hook",
                        "start_s": 3.0,
                        "end_s": 1.0,  # inverted
                        "position": "center",
                        "font_size_hint": "large",
                        "sample_text": "boom",
                    }
                ],
            }
        ]
        _validate_slots(slots, global_color_grade="none")
        assert slots[0]["text_overlays"] == []

    def test_validate_slots_drops_inf_when_slot_dur_zero(self):
        """Same direct-call scenario with inf start_s. The non-finite guard
        drops the overlay before salvage or the standard drop run."""
        slots = [
            {
                "position": 1,
                "target_duration_s": 0.0,
                "text_overlays": [
                    {
                        "role": "hook",
                        "start_s": float("inf"),
                        "end_s": 5.0,
                        "position": "center",
                        "font_size_hint": "large",
                        "sample_text": "boom",
                    }
                ],
            }
        ]
        _validate_slots(slots, global_color_grade="none")
        assert slots[0]["text_overlays"] == []

    def test_nan_start_is_dropped(self):
        """NaN comparisons all return False, so without an explicit finite
        check the overlay would slip every gate. The non-finite guard catches
        it before salvage or the standard drop checks."""
        payload = _recipe_with_overlay(start_s=float("nan"), end_s=2.0, slot_dur=10.0)
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["text_overlays"] == []

    def test_inf_end_is_dropped(self):
        """Same as NaN: an infinite end would otherwise pass the standard
        start>=end gate (since 0 >= inf is False). Caught by finite check."""
        payload = _recipe_with_overlay(start_s=0.0, end_s=float("inf"), slot_dur=10.0)
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["text_overlays"] == []
