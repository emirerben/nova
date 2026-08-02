"""Structural eval contract for TextElement schema changes.

The T8 eval-fixture guard treats agent schema edits as eval-input changes. This
test is intentionally local/replay-only: it pins the new TextElement field at
the schema boundary without requiring a live model fixture for a renderer flag.
"""

from app.agents._schemas.text_element import TextElement, _burn_dict_to_text_element


def test_text_element_shadow_enabled_false_is_valid_agent_schema_output() -> None:
    elem = TextElement.model_validate(
        {
            "id": "hero",
            "role": "generative_intro",
            "text": "Clean text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.42,
            "shadow_enabled": False,
        }
    )

    assert elem.shadow_enabled is False
    assert elem.model_dump()["shadow_enabled"] is False


def test_text_element_high_visibility_is_valid_agent_schema_output() -> None:
    elem = TextElement.model_validate(
        {
            "id": "hero",
            "role": "generative_intro",
            "text": "Readable text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.42,
            "shadow_enabled": True,
            "shadow_style": "high_visibility",
        }
    )

    assert elem.shadow_style == "high_visibility"
    assert elem.model_dump()["shadow_style"] == "high_visibility"


def test_burn_dict_adapter_preserves_shadow_enabled_false() -> None:
    elem = _burn_dict_to_text_element(
        {
            "text": "Clean text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "shadow_enabled": False,
        }
    )

    assert elem is not None
    assert elem.shadow_enabled is False


def test_burn_dict_adapter_preserves_high_visibility_shadow_style() -> None:
    elem = _burn_dict_to_text_element(
        {
            "text": "Readable text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "shadow_style": "high_visibility",
        }
    )

    assert elem is not None
    assert elem.shadow_style == "high_visibility"


def test_burn_dict_adapter_coerces_unknown_shadow_style_to_standard_default() -> None:
    elem = _burn_dict_to_text_element(
        {
            "text": "Readable text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "shadow_style": "future_style",
        }
    )

    assert elem is not None
    assert elem.shadow_style is None


def test_burn_dict_adapter_takes_the_renderers_y_for_a_half_pinned_overlay() -> None:
    """A burn dict that pins x but not y must project at the y the RENDERER uses.

    `_resolve_anchor` (text_overlay_skia.py) falls back to `_POSITION_Y[position]`
    when `position_y_frac` is absent — 0.45 for "center", 0.85 for "bottom". The
    adapter used to invent 0.5 for any partially-pinned overlay, so the projected
    element disagreed with the burn. Curated style sets ship exactly this shape
    (`word_reveal` / `typewriter` / `ai_answer` pin x = 0.06 with a null y), and
    both the intro adapter and the lyric-seed adapter read through this helper.
    """
    from app.pipeline.text_overlay import _POSITION_Y

    centered = _burn_dict_to_text_element(
        {
            "text": "Clean text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "position_x_frac": 0.06,
        }
    )
    assert centered is not None
    assert centered.position == "custom"
    assert centered.x_frac == 0.06
    assert centered.y_frac == _POSITION_Y["center"] != 0.5

    bottom = _burn_dict_to_text_element(
        {
            "text": "Clean text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "bottom",
            "position_x_frac": 0.06,
        }
    )
    assert bottom is not None
    assert bottom.y_frac == _POSITION_Y["bottom"] != 0.5


def test_burn_dict_adapter_keeps_a_zero_valued_frac() -> None:
    """0.0 is a position, not an absence — an edge-pinned overlay must stay pinned."""
    elem = _burn_dict_to_text_element(
        {
            "text": "Clean text",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "position_x_frac": 0.0,
            "position_y_frac": 0.0,
        }
    )

    assert elem is not None
    assert (elem.x_frac, elem.y_frac) == (0.0, 0.0)
