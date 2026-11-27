"""KRI-1615 stage A: photo cards laid out together with the intro title.

The scene names an arrangement only (no media id, no box); the engine fills ordinal slots from the
photos the caller issued and lays the cards and the title out together around the speaker. The
expected geometry comes from the requirement (the face, the caption row, the Kria watermark, the
frame inset, the title between the card rows, each photo's own shape), not from a run of the code.

Failure modes (intro_scene M19-M30) covered here:
* an arrangement nobody issued, or a grid from three photos, is accepted (M19);
* a media id or a coordinate reaches the scene (M20: the schema test keeps it closed);
* a mid-frame title is ignored instead of refused (M21);
* a card on the face, the captions or the watermark, cards on each other or off the frame
  (M22, M23);
* the title touches a card, leaves the band between the rows, or shrinks for them (M24, M25);
* a card drawn at another shape than its photo, portrait / landscape / square rows (M26, M27);
* a longer Turkish title, a title that cannot fit (M28, M29);
* a scene without an arrangement changes (M30).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.pipeline import intro_scene as mod

TITLE_EN = "city guide: lisbon, day one"
TITLE_TR = "şehir rehberi: lizbon, ilk gün"
VOICES = ["accent", "accent", "solid", "solid", "solid"]


def _scene(**over: Any) -> mod.IntroScene:
    return mod.IntroScene.model_validate(
        {
            "decision": "compose",
            "voices": VOICES,
            "font_pair": "script_serif",
            "ink": "white",
            "zone": "above_head",
            "entrance": "fade",
            "sound": "none",
            **over,
        }
    )


# --- M19, M21, M30: the scene, the menu and the check (skia-free) ----------------------------


@pytest.mark.parametrize(
    ("photos", "issued"),
    [
        (0, {"none"}),
        (1, {"none"}),
        (2, {"none", "around_speaker"}),
        (3, {"none", "around_speaker"}),
        (4, {"none", "around_speaker", "grid_2x2"}),
        (9, {"none", "around_speaker", "grid_2x2"}),
    ],
)
def test_an_arrangement_is_issued_only_with_a_photo_for_every_slot(photos, issued):
    assert set(mod.issue_menu(TITLE_EN, photo_count=photos).arrangements) == issued


def test_a_grid_from_three_photos_is_refused():
    grid = _scene(arrangement="grid_2x2")
    three = mod.issue_menu(TITLE_EN, photo_count=3)
    assert mod.check_scene(grid, TITLE_EN, three) == "option_not_issued"
    assert mod.check_scene(grid, TITLE_EN, mod.issue_menu(TITLE_EN, photo_count=4)) is None


def test_an_arrangement_with_a_mid_frame_title_is_refused():
    menu = mod.issue_menu(TITLE_EN, photo_count=4)
    scene = _scene(arrangement="around_speaker", zone="mid_frame")
    assert mod.check_scene(scene, TITLE_EN, menu) == "arrangement_needs_a_top_zone"
    assert mod.check_scene(_scene(zone="mid_frame"), TITLE_EN, menu) is None
    left = _scene(arrangement="around_speaker", zone="top_left")
    assert mod.check_scene(left, TITLE_EN, menu) is None


def test_the_pick_the_offer_and_the_model_issue_no_arrangement():
    from app.agents.intro_scene_designer import build_input, response_schema_for

    assert mod.issue_menu(TITLE_EN).arrangements == ("none",)
    ask = build_input(TITLE_EN, language="en")
    assert ask is not None
    assert ask.menu().arrangements == ("none",)
    assert "arrangement" not in response_schema_for(ask)["properties"]
    scene = _scene(arrangement="grid_2x2")
    assert mod.check_scene(scene, TITLE_EN, ask.menu()) == "option_not_issued"


def test_a_scene_without_an_arrangement_dumps_and_declares_as_before():
    plain = _scene()
    assert "arrangement" not in plain.model_dump(mode="json")
    assert _scene(arrangement="none").model_dump(mode="json") == plain.model_dump(mode="json")
    grid = _scene(arrangement="grid_2x2")
    assert grid.model_dump(mode="json")["arrangement"] == "grid_2x2"
    assert mod.IntroScene.model_validate(grid.model_dump(mode="json")) == grid
    assert grid.moves() == plain.moves() == {}  # the title rows never declare the cards
