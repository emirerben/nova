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

from app.agents._schemas.template_text import TextBBox
from app.agents.template_text import (
    TemplateTextInput,
    TemplateTextOutput,
    TemplateTextOverlay,
)
from app.tasks.template_text_extraction import (
    _apply_legibility_pacing,
    _bbox_to_named_position,
    _build_slot_boundaries,
    _merge_overlays_into_slots,
    _overlay_to_recipe_dict,
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
        bbox=TextBBox(x_norm=x, y_norm=y, w_norm=0.4, h_norm=0.08, sample_frame_t=start_s + 0.1),
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


# ── style-set styled path ────────────────────────────────────────────────────


def test_overlay_to_recipe_dict_styled_path() -> None:
    """With a style_set_id, the overlay carries the set id + _style_set_styled
    sentinel and DROPS the uniform Layer-2 fields (the set owns styling)."""
    ov = _make_overlay(text="Tokyo", role="hook")
    d = _overlay_to_recipe_dict(ov, style_set_id="travel_editorial")
    assert d["style_set_id"] == "travel_editorial"
    assert d["_style_set_styled"] is True
    assert "_layer2_uniform" not in d
    assert "text_size_px" not in d  # uniform sizing not pinned; set provides it
    assert d["role"] == "hook"
    # advisory fields preserved for fallback / round-trip
    assert d["font_color_hex"] == "#FFFFFF"


def test_overlay_to_recipe_dict_uniform_path_unchanged() -> None:
    """Without a style_set_id, the legacy uniform path is unchanged."""
    ov = _make_overlay(text="Tokyo", role="hook")
    d = _overlay_to_recipe_dict(ov)
    assert d.get("_layer2_uniform") is True
    assert d["text_size_px"] == 120
    assert "style_set_id" not in d


def test_merge_threads_style_set_id_onto_overlays() -> None:
    slots = [{"target_duration_s": 5.0, "text_overlays": []}]
    _merge_overlays_into_slots(slots, [_make_overlay()], style_set_id="lifestyle_clean")
    assert slots[0]["text_overlays"][0]["style_set_id"] == "lifestyle_clean"


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
    assert "STALE FROM RECIPE" not in {ov.get("sample_text") for ov in slots[0]["text_overlays"]}


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


def test_merge_overlays_uniform_styling() -> None:
    """Every Layer-2 overlay must emit the uniform-styling fields regardless of
    the per-overlay text_anchor / size_class chosen by Stage F.

    Drift in any of these fields would re-introduce the per-overlay size +
    center-anchor regression that the uniform bridge was added to fix.
    """
    slot = {"target_duration_s": 3.0, "text_overlays": []}
    overlay = TemplateTextOverlay(
        slot_index=1,
        sample_text="Hello",
        start_s=0.5,
        end_s=2.0,
        bbox=TextBBox(x_norm=0.3, y_norm=0.85, w_norm=0.4, h_norm=0.08, sample_frame_t=0.5),
        font_color_hex="#FFFFFF",
        effect="none",
        role="label",
        size_class="medium",
    )
    _merge_overlays_into_slots([slot], [overlay])
    ov = slot["text_overlays"][0]
    # Uniform-style contract: same for every Layer-2 overlay.
    assert ov["text_size"] == "large"
    assert ov["text_size_px"] == 120
    assert ov["text_anchor"] == "left"
    assert ov["position_x_frac"] == pytest.approx(0.05)
    assert ov["_layer2_uniform"] is True
    # Per-overlay vertical position survives from bbox.y_norm.
    assert ov["position_y_frac"] == pytest.approx(0.85)
    # font_size_hint is preserved as metadata for eval-fixture export round-trip
    # (renderer ignores it — text_size/text_size_px take effect).
    assert ov["font_size_hint"] == "medium"


def test_merge_overlays_preserves_pop_animated_suffix() -> None:
    """`pop_animated_suffix` (per-overlay reveal target) survives the
    uniform-styling overwrite. Only styling/positioning fields are forced
    constant; reveal animation metadata stays per-overlay.
    """
    slot = {"target_duration_s": 3.0, "text_overlays": []}
    overlay = TemplateTextOverlay(
        slot_index=1,
        sample_text="good morning",
        start_s=1.0,
        end_s=2.5,
        bbox=TextBBox(x_norm=0.15, y_norm=0.7, w_norm=0.1, h_norm=0.05, sample_frame_t=1.0),
        font_color_hex="#FFFFFF",
        effect="pop-in",
        role="label",
        size_class="medium",
        text_anchor="left",
        pop_animated_suffix="morning",
    )
    _merge_overlays_into_slots([slot], [overlay])
    ov = slot["text_overlays"][0]
    assert ov["pop_animated_suffix"] == "morning"


def test_merge_overlays_uniform_fields_invariant_across_inputs() -> None:
    """Stage G can hand us overlays with wildly varying size_class / role /
    text_anchor / bbox.x_norm. The bridge must collapse all of that variance
    into a single uniform style — only `position_y_frac` (and per-overlay
    text / timing / color) should differ across the output.
    """
    slot = {"target_duration_s": 10.0, "text_overlays": []}
    inputs = [
        TemplateTextOverlay(
            slot_index=1,
            sample_text="hook line",
            start_s=0.0,
            end_s=1.0,
            bbox=TextBBox(x_norm=0.5, y_norm=0.2, w_norm=0.3, h_norm=0.1, sample_frame_t=0.0),
            font_color_hex="#FFFFFF",
            effect="none",
            role="hook",
            size_class="jumbo",
            text_anchor="center",
        ),
        TemplateTextOverlay(
            slot_index=1,
            sample_text="reaction",
            start_s=2.0,
            end_s=3.0,
            bbox=TextBBox(x_norm=0.8, y_norm=0.6, w_norm=0.2, h_norm=0.05, sample_frame_t=2.0),
            font_color_hex="#FFFF00",
            effect="none",
            role="reaction",
            size_class="small",
            text_anchor="right",
        ),
        TemplateTextOverlay(
            slot_index=1,
            sample_text="cta",
            start_s=4.0,
            end_s=5.0,
            bbox=TextBBox(x_norm=0.2, y_norm=0.9, w_norm=0.15, h_norm=0.08, sample_frame_t=4.0),
            font_color_hex="#00FF00",
            effect="none",
            role="cta",
            size_class="large",
            text_anchor="left",
        ),
    ]
    _merge_overlays_into_slots([slot], inputs)
    overlays = slot["text_overlays"]
    assert len(overlays) == 3
    for ov in overlays:
        assert ov["text_size"] == "large"
        assert ov["text_size_px"] == 120
        assert ov["text_anchor"] == "left"
        assert ov["position_x_frac"] == pytest.approx(0.05)
        assert ov["_layer2_uniform"] is True
    # Vertical position is the one styling-adjacent field that varies.
    assert [ov["position_y_frac"] for ov in overlays] == pytest.approx([0.2, 0.6, 0.9])


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


def test_legibility_pass_floors_crammed_reveal() -> None:
    """A cumulative reveal whose words flash by below the readable floor (the
    prod 89cde014 "the work to get there." case) is expanded by
    `_apply_legibility_pacing` (run after merge) so every word clears
    MIN_PER_WORD_S, and nothing spills past the slot."""
    from app.pipeline.overlay_pacing import MIN_PER_WORD_S

    slot_dur = 6.0
    # 4 cumulative stages crammed into ~0.34 s total (global, slot 1).
    overlays = [
        _make_overlay(slot_index=1, text="the work", start_s=0.0, end_s=0.086),
        _make_overlay(slot_index=1, text="the work to", start_s=0.086, end_s=0.172),
        _make_overlay(slot_index=1, text="the work to get", start_s=0.172, end_s=0.258),
        _make_overlay(slot_index=1, text="the work to get there.", start_s=0.258, end_s=0.34),
    ]
    for ov, suffix in zip(overlays, ["work", "to", "get", "there."], strict=True):
        ov.pop_animated_suffix = suffix
    slots = [{"target_duration_s": slot_dur, "text_overlays": []}]
    _merge_overlays_into_slots(slots, overlays)
    _apply_legibility_pacing(slots)

    rendered = slots[0]["text_overlays"]
    assert len(rendered) == 4
    for ov in rendered:
        assert ov["end_s"] - ov["start_s"] >= MIN_PER_WORD_S - 1e-6
    assert max(ov["end_s"] for ov in rendered) <= slot_dur + 1e-6


def test_merge_is_timing_faithful_before_pacing() -> None:
    """`_merge_overlays_into_slots` alone must NOT retime — only convert global
    to slot-relative — so the eval-fixture export round-trip reconstructs the
    agent's true output. The pacing pass is a separate post-step."""
    overlays = [
        _make_overlay(slot_index=1, text="the work", start_s=0.0, end_s=0.086),
        _make_overlay(slot_index=1, text="the work to", start_s=0.086, end_s=0.172),
    ]
    for ov, suffix in zip(overlays, ["work", "to"], strict=True):
        ov.pop_animated_suffix = suffix
    slots = [{"target_duration_s": 6.0, "text_overlays": []}]
    _merge_overlays_into_slots(slots, overlays)
    rendered = slots[0]["text_overlays"]
    # Timings untouched by merge (slot starts at global 0, so identical).
    assert [round(o["end_s"], 3) for o in rendered] == [0.086, 0.172]


def test_legibility_pass_leaves_well_paced_reveal_unchanged() -> None:
    """The legibility pass is a no-op when every word is already readable."""
    overlays = [
        _make_overlay(slot_index=1, text="It's", start_s=0.0, end_s=0.5),
        _make_overlay(slot_index=1, text="It's not", start_s=0.5, end_s=1.0),
        _make_overlay(slot_index=1, text="It's not luck", start_s=1.0, end_s=2.0),
    ]
    for ov, suffix in zip(overlays, ["It's", "not", "luck"], strict=True):
        ov.pop_animated_suffix = suffix
    slots = [{"target_duration_s": 8.0, "text_overlays": []}]
    _merge_overlays_into_slots(slots, overlays)
    _apply_legibility_pacing(slots)

    rendered = slots[0]["text_overlays"]
    assert [round(o["start_s"], 3) for o in rendered] == [0.0, 0.5, 1.0]
    assert [round(o["end_s"], 3) for o in rendered] == [0.5, 1.0, 2.0]


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
    recipe = _StubRecipe(slots=[{"target_duration_s": 0.0, "text_overlays": []}])
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


def test_extract_transcript_words_propagates_to_agent_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transcript_words is forwarded into TemplateTextInput so Stage E of the
    Layer-2 pipeline (text alignment) actually runs against the audio
    transcript. Regression for the agentic build where the parameter was
    omitted and Stage E always short-circuited on empty transcript — letting
    OCR garbage like "if you if you put put in" leak into cached recipes.
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    transcript = [
        {"text": "if", "start_s": 3.0, "end_s": 3.2},
        {"text": "you", "start_s": 3.2, "end_s": 3.5},
        {"text": "put", "start_s": 3.5, "end_s": 3.7},
        {"text": "in", "start_s": 3.8, "end_s": 4.0},
    ]
    recipe = _StubRecipe(slots=[{"target_duration_s": 5.0, "text_overlays": []}])
    success, _ = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-transcript",
        transcript_words=transcript,
    )
    assert success is True
    # The transcript reached the agent input intact.
    received = captured_input["input"].transcript_words
    assert len(received) == 4
    # Pydantic may coerce dicts → TranscriptWord; accept either shape.
    received_texts = [(w.text if hasattr(w, "text") else w["text"]) for w in received]
    assert received_texts == ["if", "you", "put", "in"]


def test_extract_transcript_words_defaults_to_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller omits transcript_words the agent receives an empty list,
    not None. Preserves the pre-PR behavior for non-Layer-2 callers and keeps
    Stage E's empty-transcript early-return contract intact.
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    extract_template_text_overlays(_StubFileRef(), recipe, job_id="t-no-transcript")
    assert captured_input["input"].transcript_words == []


def test_extract_force_layer2_propagates_to_agent_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force_layer2=True is forwarded into the TemplateTextInput the agent receives.

    Verifies that the bridge kwarg reaches the agent's input schema so that
    TemplateTextAgent.run() can read it and route accordingly.
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    success, _ = extract_template_text_overlays(
        _StubFileRef(), recipe, job_id="t-force", force_layer2=True
    )
    assert success is True
    # The force_layer2 flag must be present and True on the input the agent received.
    assert captured_input["input"].force_layer2 is True


def test_extract_force_layer2_defaults_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When force_layer2 is not passed, it defaults to False on the agent input.

    Backward-compatibility guard: existing callers that don't pass force_layer2
    must not inadvertently activate Layer-2.
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    extract_template_text_overlays(_StubFileRef(), recipe, job_id="t-default")
    assert captured_input["input"].force_layer2 is False


def test_extract_gcs_path_propagates_to_agent_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gcs_path is forwarded into the TemplateTextInput the agent receives.

    Verifies the plumbing added in the canary-404 fix: the agentic build task
    passes template.gcs_path to extract_template_text_overlays, which in turn
    sets it on the TemplateTextInput so _run_layer2 can call download_to_file
    with the correct GCS object path instead of the Gemini File API URI.
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    success, _ = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-gcs",
        gcs_path="templates/abc/video.mp4",
    )
    assert success is True
    assert captured_input["input"].gcs_path == "templates/abc/video.mp4"


def test_extract_gcs_path_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gcs_path is not passed, it defaults to None on the agent input.

    Backward-compatibility guard: existing callers that don't pass gcs_path
    must not break. _run_layer2 handles None by raising TerminalError if
    Layer-2 is active; the bridge catches that and returns (False, 0).
    """
    from app.tasks import template_text_extraction as mod

    captured_input: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            captured_input["input"] = inp
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    extract_template_text_overlays(_StubFileRef(), recipe, job_id="t-gcs-default")
    assert captured_input["input"].gcs_path is None


# ── transcript_failed guard ──────────────────────────────────────────────────


def test_extract_skips_merge_when_transcript_failed_with_force_layer2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for prod template 89cde014 on 2026-05-22 09:13:
    `nova.audio.transcript` returned terminal_refusal, Stage E saw
    transcript_words=[] and fell into its music-only passthrough — which
    overwrote template_recipe's clean phrases with broken atomized OCR
    ('luck"', 'W', duplicates). The guard must short-circuit BEFORE the
    agent runs so the agent never gets to mutate recipe.slots.
    """
    from app.tasks import template_text_extraction as mod

    class _AgentMustNotRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            raise AssertionError(
                "extract_template_text_overlays must NOT invoke the agent "
                "when transcript_failed=True and Layer-2 is the chosen path"
            )

    monkeypatch.setattr(mod, "TemplateTextAgent", _AgentMustNotRun)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    pre_existing = [{"role": "label", "sample_text": "The work", "start_s": 0, "end_s": 1}]
    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": list(pre_existing)}])

    success, merged = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-89cde014",
        force_layer2=True,
        transcript_words=[],
        transcript_failed=True,
    )

    assert success is False  # gates the cache write upstream
    assert merged == 0
    # template_recipe's overlays must survive intact.
    assert recipe.slots[0]["text_overlays"] == pre_existing


def test_extract_skips_merge_when_transcript_failed_with_settings_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard also fires when Layer-2 is enabled via
    ``settings.text_overlay_v2_enabled=True`` (not just the admin
    force_layer2 override). Both paths must protect template_recipe overlays.
    """
    from app.tasks import template_text_extraction as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)

    class _AgentMustNotRun:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            raise AssertionError("agent must not run when transcript_failed=True")

    monkeypatch.setattr(mod, "TemplateTextAgent", _AgentMustNotRun)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    success, merged = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-flag",
        force_layer2=False,  # not the admin override
        transcript_failed=True,
    )
    assert success is False
    assert merged == 0


def test_extract_proceeds_when_transcript_failed_but_layer1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer-1 callers (no force_layer2 AND settings flag False) have no
    Stage E and therefore no transcript dependency. The transcript_failed
    signal is meaningless to them and must NOT block the agent run, or
    Layer-1 templates would stop receiving template_text overlays whenever
    a (Layer-2-only) transcribe call happened to fail.
    """
    from app.tasks import template_text_extraction as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", False)

    ran = {"count": 0}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            ran["count"] += 1
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    success, _ = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-layer1",
        force_layer2=False,
        transcript_failed=True,  # ignored on Layer-1
    )
    assert success is True
    assert ran["count"] == 1


def test_extract_proceeds_when_transcript_ok_layer2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-baseline: with transcript_failed=False, Layer-2 is unaffected
    and the agent runs as before. Locks the happy-path so the guard's
    early-return can't accidentally widen.
    """
    from app.tasks import template_text_extraction as mod

    ran = {"count": 0}

    class _StubAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, inp: TemplateTextInput, ctx=None):  # noqa: ARG002
            ran["count"] += 1
            return TemplateTextOutput(overlays=[])

    monkeypatch.setattr(mod, "TemplateTextAgent", _StubAgent)
    monkeypatch.setattr(mod, "default_client", lambda: object())

    recipe = _StubRecipe(slots=[{"target_duration_s": 3.0, "text_overlays": []}])
    success, _ = extract_template_text_overlays(
        _StubFileRef(),
        recipe,
        job_id="t-ok",
        force_layer2=True,
        transcript_words=[{"text": "ok", "start_s": 0.0, "end_s": 0.5}],
        transcript_failed=False,
    )
    assert success is True
    assert ran["count"] == 1
