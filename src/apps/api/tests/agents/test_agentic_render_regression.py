"""CRITICAL regression tests.

The IRON RULE in plan-eng-review requires regression tests for every
behavior the diff changes. This file covers:

  1. The exact 24ac3408 16:35 bad recipe — parser dedup guard merges the
     duplicate "being rich in life" overlay and the interstitial-grounding
     guard drops the fabricated 2-second fade-to-black.
  2. Classic templates are unaffected: a recipe with NO pct fields still
     parses, validates, and round-trips with the same slot and overlay
     timings it had before this PR.
  3. Legacy agentic recipes (is_agentic=True but predating the pct schema)
     still render via the inner fallback inside the agentic branch.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.template_recipe import (
    BlackSegment,
    TemplateRecipeAgent,
    TemplateRecipeInput,
)
from app.pipeline.agentic_timing import (
    resolve_overlay_window,
    resolve_slot_duration,
)

_REGRESSION_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "regression"


def _agent() -> TemplateRecipeAgent:
    return TemplateRecipeAgent(model_client=None)  # type: ignore[arg-type]


class TestRegression_24ac3408_BadRecipe:
    """The exact failure-mode recipe captured from prod on 2026-05-15 16:35.

    Trace: 28f24609-492a-4529-b455-c0dce5cd3886
    Template: 24ac3408-993e-4341-a6bb-cfcc4f5ba41c
    Symptoms: video fades to black mid-clip, then "being rich in life"
    reappears looking like the video restarted.
    """

    def _load_fixture(self) -> dict:
        path = _REGRESSION_DIR / "template_recipe_24ac3408_malformed.json"
        with path.open() as f:
            payload = json.load(f)
        # _test_meta is documentation, not part of the agent contract.
        payload.pop("_test_meta", None)
        return payload

    def test_dedup_guard_drops_duplicate_text_on_slot_2(self):
        """The duplicate 'being rich in life' overlay on slot 2 must be removed."""
        payload = self._load_fixture()
        out = _agent().parse(
            json.dumps(payload),
            TemplateRecipeInput(
                file_uri="files/test",
                file_mime="video/mp4",
                analysis_mode="single",
                creative_direction="",
                black_segments=[],  # what the live job actually saw
            ),
        )

        # Slot 1 keeps its two overlays (the duplicate originated on slot 2).
        slot_1_texts = [ov["sample_text"].lower() for ov in out.slots[0]["text_overlays"]]
        assert "this is what I call:".lower() in slot_1_texts
        assert "being rich in life" in slot_1_texts

        # Slot 2 had ONE overlay — also "being rich in life" — which is the
        # same-text-on-adjacent-slot duplicate. After the dedup pass it is
        # gone, so the rendered video doesn't visually "restart" the text.
        slot_2_texts = [ov["sample_text"] for ov in out.slots[1]["text_overlays"]]
        assert "being rich in life" not in slot_2_texts

    def test_grounding_guard_drops_fabricated_fade_to_black(self):
        """The 2-second fade-black-hold with no detected black segment must be dropped."""
        payload = self._load_fixture()
        out = _agent().parse(
            json.dumps(payload),
            TemplateRecipeInput(
                file_uri="files/test",
                file_mime="video/mp4",
                analysis_mode="single",
                creative_direction="",
                black_segments=[],
            ),
        )
        # Interstitial in the bad recipe had hold_s=2.0 with no black_segments.
        # Should be dropped post-guard.
        assert out.interstitials == []

    def test_grounding_guard_passes_with_real_black_segment(self):
        """If a black_segment is detected, the same interstitial should pass."""
        payload = self._load_fixture()
        out = _agent().parse(
            json.dumps(payload),
            TemplateRecipeInput(
                file_uri="files/test",
                file_mime="video/mp4",
                analysis_mode="single",
                creative_direction="",
                black_segments=[
                    BlackSegment(
                        start_s=15.0,
                        end_s=17.0,
                        duration_s=2.0,
                        likely_type="fade-black-hold",
                    )
                ],
            ),
        )
        assert len(out.interstitials) == 1
        assert out.interstitials[0]["type"] == "fade-black-hold"


class TestRegression_ClassicTemplateUnchanged:
    """Classic templates (is_agentic=False) must render identically to today.

    The renderer's slot_duration and overlay_window resolvers must return
    the recipe's frozen seconds verbatim when is_agentic=False, even if
    a curious recipe carries pct fields.
    """

    def test_classic_ignores_target_duration_pct(self):
        slot = {"target_duration_s": 22.0, "target_duration_pct": 0.5}
        # User clips total 100s — would scale wildly if classic respected pct.
        assert resolve_slot_duration(slot, is_agentic=False, user_total_dur_s=100.0) == 22.0

    def test_classic_ignores_overlay_pct(self):
        ov = {
            "start_s": 2.04,
            "end_s": 5.77,
            "start_pct": 0.085,
            "end_pct": 0.24,
        }
        # user-clip slot is 100s — pct math would put end at 24s.
        # Classic returns 5.77 from the recipe.
        start, end = resolve_overlay_window(ov, slot_actual_dur=100.0, is_agentic=False)
        assert start == 2.04
        assert end == 5.77


class TestRegression_LegacyAgenticInnerFallback:
    """is_agentic=True templates that predate the pct schema (no pct in
    recipe_cached) must still render. They flow through the inner fallback
    branch (A1b in the plan) that uses absolute seconds.
    """

    def test_agentic_slot_falls_back_to_seconds_when_pct_absent(self):
        slot = {"target_duration_s": 22.0}  # no target_duration_pct
        # Agentic, but recipe predates pct schema → fall back to recipe seconds.
        # User's actual clip length is irrelevant in this branch — the legacy
        # recipe didn't know about it and the renderer can't invent it.
        assert resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=42.0) == 22.0

    def test_agentic_overlay_falls_back_to_seconds_when_pct_absent(self):
        ov = {"start_s": 2.04, "end_s": 5.77}  # no pct fields
        start, end = resolve_overlay_window(ov, slot_actual_dur=42.0, is_agentic=True)
        assert start == 2.04
        assert end == 5.77
