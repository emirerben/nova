"""PR2 regression guard — manual templates produce byte-identical output.

These tests lock the invariant that adding the `is_agentic` branch to
`_collect_absolute_overlays`, `_pre_burn_curtain_slot_text`, and the
matcher dispatch in template_orchestrate cannot affect the manual path.

Strategy: call the affected functions twice on the same input — once
without an `is_agentic` kwarg (legacy callers), once with `is_agentic=False`
— and assert the two outputs are identical. Then call with `is_agentic=True`
and assert the static `_LABEL_CONFIG` override is correctly suppressed.

Full-render byte equality is out of scope for unit tests (requires real
video fixtures); the FFmpeg layer below `_assemble_clips` is unchanged
by this PR so a structural-equality test on the inputs to that layer is
the right level for a regression guard.
"""

from __future__ import annotations

import pytest

from app.tasks.template_orchestrate import (
    _collect_absolute_overlays,
    _is_subject_placeholder,
)


def _make_steps_with_label_overlay() -> tuple[list, list[float]]:
    """A single step with one subject label overlay that triggers _LABEL_CONFIG.

    "PERU" is detected as a subject placeholder (ALL-CAPS, ≤3 words),
    which triggers the `_LABEL_CONFIG["subject"]` override block in
    `_collect_absolute_overlays`. The override sets `start_s` (timing)
    and `font_cycle_accel_at_s` (acceleration) for first-slot labels.
    """

    class _Step:
        def __init__(self, slot: dict) -> None:
            self.slot = slot
            self.clip_id = "fixture-clip"
            self.moment = {"start_s": 0.0, "end_s": 5.0, "energy": 5.0, "description": ""}

    slot = {
        "position": 0,
        "slot_type": "hook",
        "text_overlays": [
            {
                "role": "label",
                "sample_text": "PERU",
                "text": "PERU",
                "start_s": 0.0,
                "end_s": 5.0,
                "text_size": "xxlarge",
                "font_style": "sans",
                "text_color": "#F4D03F",
                "effect": "font-cycle",
                "position": "center",
                "position_x_frac": 0.5,
                "position_y_frac": 0.5,
            }
        ],
    }
    return [_Step(slot)], [5.0]


def test_subject_placeholder_detection_unchanged():
    # Spot-check the helper that decides whether an overlay is a label
    # — covered by other tests but worth pinning here so this file can
    # be read top-down and the assertions below make sense.
    assert _is_subject_placeholder("PERU") is True
    assert _is_subject_placeholder("Welcome to") is False
    assert _is_subject_placeholder("discovering a hidden river") is False


def test_manual_path_default_kwarg_matches_explicit_false():
    """Legacy callers that don't pass is_agentic must get manual behavior."""
    steps, durations = _make_steps_with_label_overlay()

    default_call = _collect_absolute_overlays(steps, durations, None, subject="PERU")
    explicit_manual = _collect_absolute_overlays(
        steps, durations, None, subject="PERU", is_agentic=False
    )

    assert default_call == explicit_manual, (
        "Adding the is_agentic kwarg changed the default-kwarg output for "
        "manual templates. This is a backwards-compat break that would "
        "affect every job in production."
    )


def test_manual_path_applies_label_config_override():
    """Manual templates STILL get the _LABEL_CONFIG override for subjects.

    For a first-slot 'PERU' subject label, the override sets:
      - start_s = 3.0 (timing — the static label config)
      - font_cycle_accel_at_s = 8.0 (cycle deceleration timing)

    If this test breaks, either _LABEL_CONFIG changed (intentional, update
    expected values) OR the manual path is no longer applying it (regression
    introduced by the is_agentic branch).
    """
    steps, durations = _make_steps_with_label_overlay()
    overlays = _collect_absolute_overlays(steps, durations, None, subject="PERU", is_agentic=False)

    assert len(overlays) == 1
    o = overlays[0]
    assert o["start_s"] == pytest.approx(3.0), (
        f"Manual path lost first-slot timing override: start_s={o['start_s']}"
    )
    assert o.get("font_cycle_accel_at_s") == pytest.approx(8.0), (
        "Manual path lost font-cycle accel override for subject label."
    )


def test_agentic_path_skips_label_config_override():
    """Agentic templates have styling baked in at build time — overlay
    fields win. The job-time _LABEL_CONFIG override would otherwise
    clobber text_designer's per-slot choices.

    Same input as the manual-path test, with is_agentic=True. The overlay's
    original start_s (0.0) should survive, and accel_at_s should NOT be
    forced to 8.0 (text_designer's value baked at build time is the
    source of truth).
    """
    steps, durations = _make_steps_with_label_overlay()
    overlays = _collect_absolute_overlays(steps, durations, None, subject="PERU", is_agentic=True)

    assert len(overlays) == 1
    o = overlays[0]
    assert o["start_s"] == pytest.approx(0.0), (
        f"Agentic path applied first-slot timing override: start_s={o['start_s']}. "
        "text_designer's baked-in start_s was clobbered by _LABEL_CONFIG."
    )
    # The overlay didn't set font_cycle_accel_at_s, so the agentic path should
    # not synthesize one. (text_designer would have set it at build time;
    # absence here means the user didn't run text_designer, which is fine —
    # the field is optional in the renderer.)
    assert "font_cycle_accel_at_s" not in o or o["font_cycle_accel_at_s"] is None, (
        "Agentic path force-set font_cycle_accel_at_s via _LABEL_CONFIG."
    )


def test_collect_absolute_overlays_forwards_text_anchor_and_pop_suffix():
    """`text_anchor` and `pop_animated_suffix` from the recipe overlay must
    survive _collect_absolute_overlays untouched.

    Both fields drove a silent prod bug on template 89cde014: the Layer-2
    bridge correctly wrote `text_anchor="left"` + `position_x_frac=0.05` into
    the recipe, but the orchestrator's entry-dict construction omitted
    text_anchor, so the renderer fell back to "center" via the
    `overlay.get("text_anchor", "center")` default — re-centering Layer-2
    overlays the bridge had marked left-anchored and clipping long phrases
    off-screen. `pop_animated_suffix` had the same drop pattern.
    """

    class _Step:
        def __init__(self, slot: dict) -> None:
            self.slot = slot
            self.clip_id = "fixture-clip"
            self.moment = {"start_s": 0.0, "end_s": 5.0, "energy": 5.0, "description": ""}

    slot = {
        "position": 0,
        "slot_type": "hook",
        "text_overlays": [
            {
                "role": "reaction",
                "sample_text": "the work to get there",
                "text": "the work to get there",
                "start_s": 0.0,
                "end_s": 4.0,
                "text_size": "large",
                "text_size_px": 120,
                "font_style": "sans",
                "text_color": "#FFFFFF",
                "effect": "pop-in",
                "position": "center",
                "position_x_frac": 0.05,
                "position_y_frac": 0.44,
                "text_anchor": "left",
                "pop_animated_suffix": "there",
                "_layer2_uniform": True,
            }
        ],
    }
    overlays = _collect_absolute_overlays([_Step(slot)], [4.0], None, subject=None, is_agentic=True)
    assert len(overlays) == 1
    o = overlays[0]
    assert o.get("text_anchor") == "left", (
        f"text_anchor was dropped on the way to the renderer (got {o.get('text_anchor')!r}). "
        "Without this forward, every Layer-2 left-anchored overlay re-centers."
    )
    assert o.get("pop_animated_suffix") == "there", (
        f"pop_animated_suffix was dropped (got {o.get('pop_animated_suffix')!r})."
    )
