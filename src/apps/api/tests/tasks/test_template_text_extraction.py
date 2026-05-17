"""Integration tests for template_text_extraction merge into recipe.slots.

The text-extraction agent OVERWRITES the recipe-agent's text_overlays. Without
that precedence pinned, a regression where the recipe agent's overlays leak
through alongside text-agent overlays would double-burn text. The renderer
deduplicates by text+position, but not before the assembly plan accounts for
each overlay, so the bug would surface as off-by-N overlay counts in the admin
debug view before it shows up in the rendered output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.agents.template_text import (
    TemplateTextInput,
    TemplateTextOutput,
    TemplateTextOverlay,
)
from app.agents._schemas.template_text import TextBBox
from app.tasks.template_text_extraction import (
    _bbox_to_named_position,
    _build_slot_boundaries,
    _merge_overlays_into_slots,
    extract_template_text_overlays,
)


@dataclass
class _StubRecipe:
    """Minimal recipe stand-in carrying only the fields the helper touches."""

    slots: list[dict[str, Any]] = field(default_factory=list)


def _make_overlay(
    *,
    slot_index: int = 1,
    text: str = "Hello",
    start_s: float = 0.0,
    end_s: float = 1.5,
    x: float = 0.5,
    y: float = 0.5,
    role: str = "label",
    effect: str = "none",
    color: str = "#FFFFFF",
) -> TemplateTextOverlay:
    return TemplateTextOverlay(
        slot_index=slot_index,
        sample_text=text,
        start_s=start_s,
        end_s=end_s,
        bbox=TextBBox(
            x_norm=x, y_norm=y, w_norm=0.4, h_norm=0.08, sample_frame_t=start_s + 0.1
        ),
        font_color_hex=color,
        effect=effect,
        role=role,
        size_class="medium",
    )


# ── _build_slot_boundaries ───────────────────────────────────────────────────


def test_build_slot_boundaries_cumulative() -> None:
    slots = [
        {"target_duration_s": 3.0},
        {"target_duration_s": 4.5},
        {"target_duration_s": 2.0},
    ]
    assert _build_slot_boundaries(slots) == [
        (0.0, 3.0),
        (3.0, 7.5),
        (7.5, 9.5),
    ]


def test_build_slot_boundaries_skips_zero_duration() -> None:
    slots = [
        {"target_duration_s": 3.0},
        {"target_duration_s": 0.0},
        {"target_duration_s": 2.0},
    ]
    # Degenerate slot is skipped — boundaries collapse, second real slot
    # starts at 3.0 (right after the first).
    assert _build_slot_boundaries(slots) == [(0.0, 3.0), (3.0, 5.0)]


def test_build_slot_boundaries_empty() -> None:
    assert _build_slot_boundaries([]) == []


# ── _bbox_to_named_position ──────────────────────────────────────────────────


def test_bbox_to_named_position_buckets() -> None:
    assert _bbox_to_named_position(0.1) == "top"
    assert _bbox_to_named_position(0.32) == "top"
    assert _bbox_to_named_position(0.5) == "center"
    assert _bbox_to_named_position(0.67) == "center"
    assert _bbox_to_named_position(0.68) == "bottom"
    assert _bbox_to_named_position(0.95) == "bottom"


# ── _merge_overlays_into_slots ───────────────────────────────────────────────


def test_merge_overlays_overwrites_existing_text_overlays() -> None:
    """The text agent's overlays replace whatever the recipe agent put there."""
    slots = [
        {
            "target_duration_s": 3.0,
            "text_overlays": [
                # Pre-existing recipe-agent overlay — must be discarded.
                {"role": "label", "sample_text": "STALE FROM RECIPE", "start_s": 0.0, "end_s": 1.0}
            ],
        },
        {"target_duration_s": 4.0, "text_overlays": []},
    ]
    overlays = [
        _make_overlay(slot_index=1, text="from text agent", start_s=0.5, end_s=2.0),
        _make_overlay(slot_index=2, text="second slot text", start_s=3.5, end_s=6.0),
    ]
    merged = _merge_overlays_into_slots(slots, overlays)
    assert merged == 2

    # First slot: only the text-agent overlay survives, recipe-agent overlay is gone.
    assert len(slots[0]["text_overlays"]) == 1
    assert slots[0]["text_overlays"][0]["sample_text"] == "from text agent"
    assert "STALE FROM RECIPE" not in {
        ov.get("sample_text") for ov in slots[0]["text_overlays"]
    }


def test_merge_overlays_converts_global_to_slot_relative_timing() -> None:
    """Text agent emits global timestamps; merge converts to slot-relative."""
    slots = [
        {"target_duration_s": 3.0, "text_overlays": []},
        {"target_duration_s": 4.0, "text_overlays": []},
    ]
    overlays = [
        # slot 1 starts at global 0.0; this overlay at global 0.5-2.0 is
        # slot-relative 0.5-2.0
        _make_overlay(slot_index=1, start_s=0.5, end_s=2.0),
        # slot 2 starts at global 3.0; this overlay at global 4.0-6.0 is
        # slot-relative 1.0-3.0
        _make_overlay(slot_index=2, start_s=4.0, end_s=6.0),
    ]
    _merge_overlays_into_slots(slots, overlays)

    assert slots[0]["text_overlays"][0]["start_s"] == pytest.approx(0.5)
    assert slots[0]["text_overlays"][0]["end_s"] == pytest.approx(2.0)
    assert slots[1]["text_overlays"][0]["start_s"] == pytest.approx(1.0)
    assert slots[1]["text_overlays"][0]["end_s"] == pytest.approx(3.0)


def test_merge_overlays_clamps_end_to_slot_duration() -> None:
    """An overlay extending past the slot end is clamped at the slot boundary."""
    slots = [{"target_duration_s": 2.0, "text_overlays": []}]
    overlays = [
        _make_overlay(slot_index=1, start_s=0.0, end_s=5.0),  # past slot end
    ]
    _merge_overlays_into_slots(slots, overlays)
    assert slots[0]["text_overlays"][0]["end_s"] == pytest.approx(2.0)


def test_merge_overlays_preserves_bbox_and_color() -> None:
    """bbox and font_color_hex make it through unchanged so the renderer can
    consume them (PR-2 wiring) and the admin debug view can display them."""
    slots = [{"target_duration_s": 3.0, "text_overlays": []}]
    overlays = [
        _make_overlay(
            slot_index=1,
            start_s=0.5,
            end_s=2.0,
            x=0.25,
            y=0.85,
            color="#F4D03F",
        )
    ]
    _merge_overlays_into_slots(slots, overlays)
    ov = slots[0]["text_overlays"][0]
    assert ov["text_bbox"]["x_norm"] == pytest.approx(0.25)
    assert ov["text_bbox"]["y_norm"] == pytest.approx(0.85)
    assert ov["font_color_hex"] == "#F4D03F"
    # bbox y=0.85 should derive named position="bottom"
    assert ov["position"] == "bottom"


def test_merge_overlays_converts_sample_frame_t_to_slot_relative() -> None:
    """sample_frame_t was global on input; merge must convert it to slot-
    relative AND keep it inside the (clamped) overlay window so the existing
    bbox invariant `start_s <= sample_frame_t <= end_s` still holds after
    merge. Previously this field was left global, breaking validate_text_bbox
    downstream."""
    slots = [
        {"target_duration_s": 3.0, "text_overlays": []},
        {"target_duration_s": 4.0, "text_overlays": []},
    ]
    # slot 2 starts at global 3.0; overlay window 4.0-6.0 global =
    # slot-relative 1.0-3.0. sample_frame_t=5.0 global = 2.0 slot-relative.
    overlay = _make_overlay(slot_index=2, start_s=4.0, end_s=6.0)
    overlay.bbox.sample_frame_t = 5.0  # global, inside the window
    _merge_overlays_into_slots(slots, [overlay])
    ov = slots[1]["text_overlays"][0]
    assert ov["start_s"] == pytest.approx(1.0)
    assert ov["end_s"] == pytest.approx(3.0)
    assert ov["text_bbox"]["sample_frame_t"] == pytest.approx(2.0)
    # Invariant: start_s <= sample_frame_t <= end_s.
    assert ov["start_s"] <= ov["text_bbox"]["sample_frame_t"] <= ov["end_s"]


def test_merge_overlays_clamps_sample_frame_t_into_clamped_window() -> None:
    """When the slot boundary pulls end_s in, sample_frame_t must be clamped
    too so it doesn't end up after the new end_s."""
    slots = [{"target_duration_s": 2.0, "text_overlays": []}]
    # Overlay extends past slot end (5.0 > 2.0). sample_frame_t=4.0 would
    # become slot-relative 4.0, but end_s clamps to 2.0. sample_frame_t must
    # follow the clamp.
    overlay = _make_overlay(slot_index=1, start_s=0.0, end_s=5.0)
    overlay.bbox.sample_frame_t = 4.0
    _merge_overlays_into_slots(slots, [overlay])
    ov = slots[0]["text_overlays"][0]
    assert ov["end_s"] == pytest.approx(2.0)
    assert ov["text_bbox"]["sample_frame_t"] <= ov["end_s"]
    assert ov["text_bbox"]["sample_frame_t"] >= ov["start_s"]


def test_merge_overlays_empty_input_clears_slot_overlays() -> None:
    """Text agent returning [] means no text on the template — slots become empty.

    This is INTENTIONAL: the text agent is the source of truth. If it sees no
    text, we trust it (the eval will catch the recall regression). Preserving
    recipe-agent overlays as a fallback would mask exactly the failure mode
    this agent exists to fix.
    """
    slots = [
        {
            "target_duration_s": 3.0,
            "text_overlays": [{"role": "label", "sample_text": "from recipe"}],
        },
    ]
    _merge_overlays_into_slots(slots, [])
    assert slots[0]["text_overlays"] == []


# ── extract_template_text_overlays (top-level helper) ─────────────────────────


class _StubFileRef:
    def __init__(self, uri: str = "files/test-123", mime: str = "video/mp4") -> None:
        self.uri = uri
        self.mime_type = mime


def test_extract_skips_when_no_file_ref(caplog: pytest.LogCaptureFixture) -> None:
    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    assert extract_template_text_overlays(None, recipe, job_id="t1") == (False, 0)
    # Slots untouched.
    assert recipe.slots[0]["text_overlays"] == []


def test_extract_skips_when_no_slots() -> None:
    recipe = _StubRecipe(slots=[])
    assert extract_template_text_overlays(_StubFileRef(), recipe, job_id="t1") == (False, 0)


def test_extract_skips_when_all_slots_zero_duration() -> None:
    recipe = _StubRecipe(
        slots=[{"target_duration_s": 0.0, "text_overlays": []}]
    )
    assert extract_template_text_overlays(_StubFileRef(), recipe, job_id="t1") == (False, 0)


def test_extract_agent_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TerminalError from the agent must NOT abort the agentic build —
    text extraction is best-effort. Recipe-agent overlays remain.
    """
    from app.agents._runtime import TerminalError
    from app.tasks import template_text_extraction as mod

    class _FailingAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            raise TerminalError("simulated agent failure")

    monkeypatch.setattr(mod, "TemplateTextAgent", _FailingAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    pre_existing = [{"role": "label", "sample_text": "from recipe", "start_s": 0, "end_s": 1}]
    recipe = _StubRecipe(
        slots=[
            {"target_duration_s": 3.0, "text_overlays": list(pre_existing)},
        ]
    )
    success, merged = extract_template_text_overlays(_StubFileRef(), recipe, job_id="t1")
    assert success is False  # gates the cache write upstream
    assert merged == 0
    # On agent failure we leave the slot untouched — recipe overlays survive.
    assert recipe.slots[0]["text_overlays"] == pre_existing


def test_extract_success_overwrites_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: agent returns overlays, merge replaces recipe-agent overlays."""
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(
                overlays=[
                    _make_overlay(slot_index=1, text="agent-text-1", start_s=0.2, end_s=1.5),
                    _make_overlay(slot_index=2, text="agent-text-2", start_s=3.5, end_s=5.5),
                ]
            )

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(
        slots=[
            {
                "target_duration_s": 3.0,
                "text_overlays": [{"role": "label", "sample_text": "recipe-stale"}],
            },
            {"target_duration_s": 4.0, "text_overlays": []},
        ]
    )
    success, merged = extract_template_text_overlays(_StubFileRef(), recipe, job_id="t1")
    assert success is True
    assert merged == 2

    # Agent received the correct slot boundaries.
    assert captured_input["input"].slot_boundaries_s == [(0.0, 3.0), (3.0, 7.0)]

    # Recipe-agent overlay is gone; agent overlays are present.
    s1_texts = [ov["sample_text"] for ov in recipe.slots[0]["text_overlays"]]
    s2_texts = [ov["sample_text"] for ov in recipe.slots[1]["text_overlays"]]
    assert s1_texts == ["agent-text-1"]
    assert s2_texts == ["agent-text-2"]
    assert "recipe-stale" not in s1_texts + s2_texts
