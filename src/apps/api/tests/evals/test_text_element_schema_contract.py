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
