"""TemplateRecipeAgent.parse() unit tests for the pct schema additions.

Covers:
  * pct fields validated, kept, dropped if invalid
  * pct + seconds coexist
  * absence of pct doesn't break the legacy schema
  * target_duration_pct validated independently of overlay pct
  * D2 dedup guard merges adjacent same-text overlays
  * D2 interstitial guard drops hallucinated black holds when no
    black_segment was detected

No Gemini, no network. parse() is pure JSON-in → schema-out.
"""

from __future__ import annotations

import json

from app.agents.template_recipe import (
    BlackSegment,
    TemplateRecipeAgent,
    TemplateRecipeInput,
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


def _well_formed_recipe(**overrides) -> dict:
    """Smallest valid recipe payload for parse(). Returns dict (not JSON)."""
    payload = {
        "shot_count": 1,
        "total_duration_s": 20.0,
        "hook_duration_s": 5.0,
        "creative_direction": "",
        "copy_tone": "casual",
        "caption_style": "",
        "slots": [
            {
                "position": 1,
                "target_duration_s": 20.0,
                "slot_type": "broll",
                "energy": 5.0,
                "text_overlays": [],
            }
        ],
        "interstitials": [],
    }
    payload.update(overrides)
    return payload


class TestPctSchemaAccepted:
    def test_target_duration_pct_kept_when_valid(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["target_duration_pct"] = 1.0
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["target_duration_pct"] == 1.0

    def test_target_duration_pct_dropped_when_out_of_range(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["target_duration_pct"] = 1.5
        out = _agent().parse(json.dumps(payload), _input())
        assert "target_duration_pct" not in out.slots[0]

    def test_target_duration_pct_dropped_when_zero(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["target_duration_pct"] = 0.0
        out = _agent().parse(json.dumps(payload), _input())
        assert "target_duration_pct" not in out.slots[0]

    def test_target_duration_pct_dropped_when_not_numeric(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["target_duration_pct"] = "half"
        out = _agent().parse(json.dumps(payload), _input())
        assert "target_duration_pct" not in out.slots[0]

    def test_overlay_pct_kept_when_valid(self):
        payload = _well_formed_recipe()
        # F8 invariant requires slot-level pct to be present for overlay
        # pct to survive — pct uniformity is enforced across the recipe.
        payload["slots"][0]["target_duration_pct"] = 1.0
        payload["slots"][0]["text_overlays"] = [
            {
                "role": "hook",
                "start_s": 2.0,
                "end_s": 5.0,
                "start_pct": 0.1,
                "end_pct": 0.25,
                "position": "center",
                "font_size_hint": "large",
                "sample_text": "wait for it",
            }
        ]
        out = _agent().parse(json.dumps(payload), _input())
        ov = out.slots[0]["text_overlays"][0]
        assert ov["start_pct"] == 0.1
        assert ov["end_pct"] == 0.25
        # Seconds fields also retained.
        assert ov["start_s"] == 2.0
        assert ov["end_s"] == 5.0

    def test_overlay_pct_dropped_when_out_of_range(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["text_overlays"] = [
            {
                "role": "hook",
                "start_s": 2.0,
                "end_s": 5.0,
                "start_pct": -0.1,
                "end_pct": 1.2,
                "position": "center",
                "font_size_hint": "large",
                "sample_text": "wait",
            }
        ]
        out = _agent().parse(json.dumps(payload), _input())
        ov = out.slots[0]["text_overlays"][0]
        assert "start_pct" not in ov
        assert "end_pct" not in ov
        # Overlay still validates via seconds — it's not dropped entirely.
        assert ov["start_s"] == 2.0

    def test_overlay_pct_dropped_when_only_start_present(self):
        payload = _well_formed_recipe()
        payload["slots"][0]["text_overlays"] = [
            {
                "role": "hook",
                "start_s": 2.0,
                "end_s": 5.0,
                "start_pct": 0.1,
                # no end_pct
                "position": "center",
                "font_size_hint": "large",
                "sample_text": "wait",
            }
        ]
        out = _agent().parse(json.dumps(payload), _input())
        ov = out.slots[0]["text_overlays"][0]
        assert "start_pct" not in ov

    def test_overlay_without_pct_unaffected(self):
        """Classic / legacy: no pct in payload → no pct in output."""
        payload = _well_formed_recipe()
        payload["slots"][0]["text_overlays"] = [
            {
                "role": "hook",
                "start_s": 2.0,
                "end_s": 5.0,
                "position": "center",
                "font_size_hint": "large",
                "sample_text": "wait",
            }
        ]
        out = _agent().parse(json.dumps(payload), _input())
        ov = out.slots[0]["text_overlays"][0]
        assert "start_pct" not in ov


class TestDedupOverlayGuard:
    """The D2 dedup guard that catches the 24ac3408 16:35 failure pattern.

    Recipe v2 had:
      slot 1: text_overlays = [hook 0→6s "this is what I call:",
                               label 6→15s "being rich in life"]
      slot 2: text_overlays = [label 0→3s "being rich in life"]  ← duplicate

    The guard should drop the duplicate on slot 2.
    """

    def test_drops_duplicate_same_text_same_position_on_next_slot(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=24.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 15.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.0,
                            "end_s": 6.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "this is what I call:",
                        },
                        {
                            "role": "label",
                            "start_s": 6.0,
                            "end_s": 15.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "being rich in life",
                        },
                    ],
                },
                {
                    "position": 2,
                    "target_duration_s": 7.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [
                        # This is the duplicate that produced the visual
                        # "video ends and shows up again" effect.
                        {
                            "role": "label",
                            "start_s": 0.0,
                            "end_s": 3.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "being rich in life",
                        }
                    ],
                },
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        # Slot 1 retains its two overlays.
        assert len(out.slots[0]["text_overlays"]) == 2
        # Slot 2's duplicate is dropped.
        assert out.slots[1]["text_overlays"] == []

    def test_keeps_different_text_on_adjacent_slots(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=20.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 10.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "first",
                        }
                    ],
                },
                {
                    "position": 2,
                    "target_duration_s": 10.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [
                        {
                            "role": "label",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "second",
                        }
                    ],
                },
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[1]["text_overlays"][0]["sample_text"] == "second"

    def test_keeps_same_text_different_position(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=20.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 10.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "position": "top",
                            "font_size_hint": "large",
                            "sample_text": "credit",
                        }
                    ],
                },
                {
                    "position": 2,
                    "target_duration_s": 10.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [
                        {
                            "role": "label",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "position": "bottom",  # different position
                            "font_size_hint": "large",
                            "sample_text": "credit",
                        }
                    ],
                },
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        # Same text but different on-screen position is a different overlay.
        assert len(out.slots[1]["text_overlays"]) == 1


class TestFlashWhiteSpared:
    """F4 fix: grounding guard skips interstitial types that aren't black frames."""

    def test_flash_white_with_long_hold_kept_without_black_segments(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=24.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 12.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 12.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [],
                },
            ],
            interstitials=[
                {
                    "after_slot": 1,
                    "type": "flash-white",
                    "animate_s": 0.1,
                    "hold_s": 1.5,
                    "hold_color": "#FFFFFF",
                }
            ],
        )
        # No black_segments — but a flash-white shouldn't be in black_segments
        # anyway. Grounding guard must skip it entirely.
        out = _agent().parse(json.dumps(payload), _input(black_segments=[]))
        assert len(out.interstitials) == 1
        assert out.interstitials[0]["type"] == "flash-white"


class TestPctUniformityEnforcement:
    """F8 fix: partial pct emission strips pct from all slots."""

    def test_partial_slot_pct_strips_all(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=20.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 10.0,
                    "target_duration_pct": 0.5,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 10.0,
                    # NO target_duration_pct — breaks uniformity
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [],
                },
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        for slot in out.slots:
            assert "target_duration_pct" not in slot

    def test_partial_overlay_pct_strips_all(self):
        payload = _well_formed_recipe(
            shot_count=1,
            total_duration_s=10.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 10.0,
                    "target_duration_pct": 1.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "start_pct": 0.0,
                            "end_pct": 0.5,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "first",
                        },
                        {
                            "role": "label",
                            "start_s": 5.0,
                            "end_s": 10.0,
                            # NO pct fields on this one — breaks uniformity
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "second",
                        },
                    ],
                }
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        # Both overlays should have lost pct, and the slot too.
        assert "target_duration_pct" not in out.slots[0]
        for ov in out.slots[0]["text_overlays"]:
            assert "start_pct" not in ov
            assert "end_pct" not in ov

    def test_full_pct_kept_when_uniform(self):
        payload = _well_formed_recipe(
            shot_count=1,
            total_duration_s=10.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 10.0,
                    "target_duration_pct": 1.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.0,
                            "end_s": 5.0,
                            "start_pct": 0.0,
                            "end_pct": 0.5,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "uniform",
                        },
                    ],
                }
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        assert out.slots[0]["target_duration_pct"] == 1.0
        ov = out.slots[0]["text_overlays"][0]
        assert ov["start_pct"] == 0.0
        assert ov["end_pct"] == 0.5

    def test_no_pct_anywhere_unchanged(self):
        """Classic recipe (no pct anywhere) doesn't get any 'partial' warning."""
        payload = _well_formed_recipe()  # No pct fields
        out = _agent().parse(json.dumps(payload), _input())
        for slot in out.slots:
            assert "target_duration_pct" not in slot
            for ov in slot.get("text_overlays", []):
                assert "start_pct" not in ov


class TestDedupMergesInsteadOfDrops:
    """F7 fix: dedup extends slot N's overlay end rather than dropping slot N+1's.

    This preserves designer intent — the text was meant to stay visible across
    the slot boundary — and matches the renderer's existing same-text merge.
    """

    def test_dedup_extends_slot_a_end_by_duplicate_duration(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=20.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 15.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [
                        {
                            "role": "label",
                            "start_s": 6.0,
                            "end_s": 15.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "being rich in life",
                        }
                    ],
                },
                {
                    "position": 2,
                    "target_duration_s": 5.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [
                        {
                            "role": "label",
                            "start_s": 0.0,
                            "end_s": 3.0,
                            "position": "center",
                            "font_size_hint": "large",
                            "sample_text": "being rich in life",
                        }
                    ],
                },
            ],
        )
        out = _agent().parse(json.dumps(payload), _input())
        # Slot 1 overlay end extended by 3s (the duration the dup occupied on slot 2).
        assert out.slots[0]["text_overlays"][0]["end_s"] == 18.0  # 15.0 + 3.0
        # Slot 2's duplicate is gone.
        assert out.slots[1]["text_overlays"] == []


class TestInterstitialGroundingGuard:
    """D2 guard: drop interstitials with hold_s >= 0.5s when no black_segment
    in the input supports them. This is exactly what produced the 24ac3408
    fake fade-to-black: Gemini emitted hold_s=2.0 with black_segments=[].
    """

    def test_drops_hallucinated_long_hold_when_no_black_segments(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=24.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 15.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 7.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [],
                },
            ],
            interstitials=[
                {
                    "after_slot": 1,
                    "type": "fade-black-hold",
                    "animate_s": 0.0,
                    "hold_s": 2.0,
                    "hold_color": "#000000",
                }
            ],
        )
        # Empty black_segments — Gemini's interstitial has no grounding.
        out = _agent().parse(json.dumps(payload), _input(black_segments=[]))
        assert out.interstitials == []

    def test_keeps_short_interstitial_without_black_segments(self):
        """Brief rhythmic black emphasis (< 0.5s hold) passes through —
        a 2-4 frame black is a stylistic choice the editor may add
        without it being a true fade."""
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=24.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 12.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 12.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [],
                },
            ],
            interstitials=[
                {
                    "after_slot": 1,
                    "type": "fade-black-hold",
                    "animate_s": 0.0,
                    "hold_s": 0.15,  # below 0.5 threshold
                    "hold_color": "#000000",
                }
            ],
        )
        out = _agent().parse(json.dumps(payload), _input(black_segments=[]))
        assert len(out.interstitials) == 1

    def test_keeps_interstitial_when_black_segment_detected(self):
        payload = _well_formed_recipe(
            shot_count=2,
            total_duration_s=24.0,
            slots=[
                {
                    "position": 1,
                    "target_duration_s": 12.0,
                    "slot_type": "hook",
                    "energy": 5.0,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 12.0,
                    "slot_type": "outro",
                    "energy": 3.0,
                    "text_overlays": [],
                },
            ],
            interstitials=[
                {
                    "after_slot": 1,
                    "type": "fade-black-hold",
                    "animate_s": 0.5,
                    "hold_s": 2.0,
                    "hold_color": "#000000",
                }
            ],
        )
        # FFmpeg detected a real black segment — interstitial is grounded.
        out = _agent().parse(
            json.dumps(payload),
            _input(
                black_segments=[
                    BlackSegment(
                        start_s=12.0,
                        end_s=14.0,
                        duration_s=2.0,
                        likely_type="fade-black-hold",
                    )
                ]
            ),
        )
        assert len(out.interstitials) == 1
