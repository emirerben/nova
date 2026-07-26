"""Replay-only eval contract for the shared dissolve-out component effect.

The eval-fixture gate treats agent schema edits as eval-input changes. These
tests pin the authoring schema boundary for the opt-in dissolve-out effect
without requiring live model calls or generated media fixtures.
"""

import pytest
from pydantic import ValidationError

from app.agents._schemas.media_overlay import MediaOverlay
from app.agents._schemas.text_element import TextElement


def _media_card(**kw) -> dict:
    base = {
        "id": "overlay-card",
        "kind": "image",
        "src_gcs_path": "users/u1/plan/p1/overlays/card.png",
        "position": "center",
        "scale": 0.35,
        "start_s": 0.0,
        "end_s": 3.0,
        "z": 0,
    }
    base.update(kw)
    return base


def test_text_element_accepts_dissolve_out_effect() -> None:
    elem = TextElement.model_validate(
        {
            "id": "hero",
            "role": "generative_intro",
            "text": "Dissolve me",
            "start_s": 0.0,
            "end_s": 3.0,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.42,
            "effect": "dissolve-out",
        }
    )

    assert elem.effect == "dissolve-out"
    assert elem.model_dump()["effect"] == "dissolve-out"


def test_text_element_rejects_unknown_effect() -> None:
    with pytest.raises(ValidationError):
        TextElement.model_validate(
            {
                "id": "hero",
                "role": "generative_intro",
                "text": "Unknown effect",
                "start_s": 0.0,
                "end_s": 3.0,
                "position": "center",
                "effect": "sparkle-away",
            }
        )


def test_media_overlay_accepts_dissolve_out_exit_token() -> None:
    card = MediaOverlay.model_validate(_media_card(exit_token="dissolve-out"))

    assert card.exit_token == "dissolve-out"
    assert card.model_dump()["exit_token"] == "dissolve-out"


def test_media_overlay_unknown_exit_token_coerces_to_none() -> None:
    card = MediaOverlay.model_validate(_media_card(exit_token="sparkle-away"))

    assert card.exit_token == "none"
    assert card.model_dump()["exit_token"] == "none"
