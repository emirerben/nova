"""KRI-1307 (review of 88ca72bb7): a recording filled to the frame and aimed at its text column IS
zoomed onto the text, so the "zoom into the recordings" receipt can say so.

88ca72bb7 floors the measured background zoom at the cover fit (1.0x) so a 16:9 README fills the
9:16 frame with no black bars. `SpeakerBackgroundLayer.all_pictures_zoomed` (and the lane
receipt's ``aimed``, which the brief check reads) only counted ``zoom > 1.0``, so her five Q2
recordings -- cover-filled (about 3.2x a contain fit) and aimed at the column (x ~0.26) -- would
read "not zoomed" forever.

Ways this can fail:

* F1 a cover-filled recording aimed at its column reads "not zoomed" (the bug).
* F2 an un-aimed centre 1.0x picture (what every background drew before item 18) reads zoomed.
* F3 a picture zoomed OUT below cover (black bars), aimed or not, reads zoomed.
* F4 one picture left without a focus no longer spoils "every picture" (it must).
* F5 the real chain (her measured columns -> the compile's focus resolver -> the recipe layer)
  disagrees with the rule.
"""

from __future__ import annotations

import pytest

from app.kria.portable_speaker_background import (
    SpeakerBackgroundFocus,
    SpeakerBackgroundLayer,
    focus_zooms_onto_text,
)


def _layer(*focuses):
    fill, *windows = focuses
    return SpeakerBackgroundLayer.model_validate(
        {
            "asset_id": "m0",
            "media_kind": "video",
            **({"focus": fill} if fill is not None else {}),
            "windows": [
                {
                    "asset_id": f"m{i + 1}",
                    "media_kind": "video",
                    "start": i * 2.0,
                    "end": i * 2.0 + 2.0,
                    **({"focus": focus} if focus is not None else {}),
                }
                for i, focus in enumerate(windows)
            ],
        }
    )


AIMED_COVER = {"x": 0.264, "y": 0.5, "zoom": 1.0}  # her spec-kit recording after 88ca72bb7


@pytest.mark.parametrize(
    "focus,zoomed",
    [
        (AIMED_COVER, True),  # F1
        ({"x": 0.0, "y": 0.5, "zoom": 1.0}, True),  # a column at the left edge
        ({"x": 0.5, "y": 0.5, "zoom": 1.0}, False),  # F2: today's centre crop
        ({"x": 0.5, "y": 0.5, "zoom": 1.4}, True),  # zoomed in, centred
        ({"x": 0.32, "y": 0.5, "zoom": 0.72}, False),  # F3: zoomed out, bars
        ({"x": 0.5, "y": 0.5, "zoom": 0.72}, False),  # F3
        (None, False),
    ],
)
def test_focus_zooms_onto_text(focus, zoomed):
    model = SpeakerBackgroundFocus.model_validate(focus) if focus is not None else None
    assert focus_zooms_onto_text(model) is zoomed
    assert focus_zooms_onto_text(focus) is zoomed  # the editor document's plain dict too


def test_every_recording_cover_filled_and_aimed_is_zoomed():
    assert _layer(AIMED_COVER, AIMED_COVER, {"x": 0.258, "y": 0.5, "zoom": 1.0}).all_pictures_zoomed


def test_one_centre_or_unfocused_recording_is_not():
    centre = {"x": 0.5, "y": 0.5, "zoom": 1.0}
    assert _layer(AIMED_COVER, AIMED_COVER, centre).all_pictures_zoomed is False  # F2
    assert _layer(AIMED_COVER, AIMED_COVER, None).all_pictures_zoomed is False  # F4


def test_her_measured_recordings_through_the_compile_resolver_read_zoomed():
    """F5: her five Q2 recordings' measured text columns (1920x1080), through the compile's own
    focus resolver, into the recipe layer."""
    from app.pipeline.canvas import Canvas
    from app.pipeline.phone_subtitled_plan import _FocusResolver
    from app.services.background_text_focus import TextColumn

    columns = {
        "spec-kit": (0.2002, 0.6006, 0.03333),
        "impeccable": (0.1960, 0.6350, 0.02407),
        "tradingagents": (0.1981, 0.6257, 0.02963),
        "agents": (0.1971, 0.6194, 0.02778),
        "game-studios": (0.2002, 0.5829, 0.02593),
    }

    class _Visual:
        def __init__(self, media_id, column):
            self.media_id = media_id
            left, right, pitch = column
            self.text_column = TextColumn(
                left=left, right=right, line_pitch=pitch, source_width=1920, source_height=1080
            )

    visuals = tuple(_Visual(name, column) for name, column in columns.items())
    resolve = _FocusResolver(visuals, Canvas(width=1080, height=1920), open_=True)
    focuses = [resolve(name, None).model_dump() for name in columns]
    assert all(f["zoom"] >= 1.0 for f in focuses)  # filled (88ca72bb7)
    assert _layer(*focuses).all_pictures_zoomed is True
