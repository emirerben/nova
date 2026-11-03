"""Green 3 (KRI-1177 / KRI-1164): the brief writer is taught the typed caption look and no_music.

Prod job 9bad2d75 stored "Captions small, white, lowercase, in the middle." with only an
untaught `position` and "No music." with no facts at all, so nothing downstream could apply or
judge them. Both the Main Creator and the narrow brief extractor share this section. Static
checks only (failure modes F5, P2); whether a live model writes the facts is a paid check.
"""

from __future__ import annotations

import pytest

from app.agents import brief_extractor, main_creator
from app.kria.caption_look_ask import CAPTION_ASK_CASES, CAPTION_ASK_POSITIONS, CAPTION_ASK_SIZE_PX

SECTIONS = {
    "main_creator": main_creator._BRIEF_PROMPT_SECTION,
    "brief_extractor": brief_extractor._NARROW_BRIEF_SECTION,
}


@pytest.mark.parametrize("name", sorted(SECTIONS))
def test_the_caption_look_keys_and_values_are_taught(name):
    section = SECTIONS[name]
    for key in ("caption_size", "caption_position", "caption_color", "caption_case"):
        assert f'"{key}"' in section, key
    for value in (*CAPTION_ASK_SIZE_PX, *CAPTION_ASK_POSITIONS, *CAPTION_ASK_CASES):
        assert f'"{value}"' in section, value
    # The Green 3 sentence is the worked example, with every typed value it carries.
    assert "Captions small, white, lowercase, in the middle" in section
    assert '"caption_color": "#FFFFFF"' in section


@pytest.mark.parametrize("name", sorted(SECTIONS))
def test_the_speakers_corner_is_never_a_caption_position(name):
    assert "never a caption position" in SECTIONS[name]


@pytest.mark.parametrize("name", sorted(SECTIONS))
def test_no_music_is_a_typed_audio_intent_and_never_a_mute(name):
    section = SECTIONS[name]
    assert '{"audio_intent": "no_music"}' in section
    assert "never mutes the footage's own sound" in section
