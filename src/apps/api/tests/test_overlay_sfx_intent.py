"""Overlay auto-SFX intent mapping against a large library (KRI-173).

Auto-apply can bake this pick into a downloaded video, so it must be
deterministic, match whole words, and never choose a voice clip.
"""

from __future__ import annotations

from app.services.overlay_autoplace import map_sfx_intent


def _fx(effect_id: str, name: str, **extra: object) -> dict:
    return {
        "id": effect_id,
        "name": name,
        "audio_gcs_path": f"sound-effects/{effect_id}/audio.m4a",
        "duration_s": 0.3,
        "role_tags": extra.get("role_tags", []),
        "contains_voice": extra.get("contains_voice", False),
    }


def test_tap_does_not_match_tape() -> None:
    glossary = [_fx("rewind", "Tape rewind"), _fx("stop", "Tape stop"), _fx("tap", "Tap")]
    assert map_sfx_intent("click", glossary)["sound_effect_id"] == "tap"


def test_role_built_for_the_intent_wins_over_name_words() -> None:
    glossary = [
        _fx("bubble", "Bubble pop"),
        _fx("accent", "Accent pop"),
        _fx("smart-soft", "Smart soft pop", role_tags=["visual_enter_soft"]),
        _fx("smart-whip", "Smart clean whoosh", role_tags=["transition_whip"]),
    ]
    assert map_sfx_intent("pop_in", glossary)["sound_effect_id"] == "smart-soft"
    assert map_sfx_intent("whoosh", glossary)["sound_effect_id"] == "smart-whip"


def test_voice_clips_are_never_picked_even_as_fallback() -> None:
    glossary = [_fx("laugh", "Pop laugh", contains_voice=True), _fx("pop", "Soft pop")]
    assert map_sfx_intent("pop_in", glossary)["sound_effect_id"] == "pop"
    only_voice = [_fx("cheer", "Crowd cheer", contains_voice=True)]
    assert map_sfx_intent("pop_in", only_voice) is None


def test_no_name_match_falls_back_to_first_usable_effect() -> None:
    glossary = [_fx("fah", "Fah"), _fx("ding", "Correct ding")]
    assert map_sfx_intent("pop_in", glossary)["sound_effect_id"] == "fah"
    assert map_sfx_intent("pop_in", glossary, allow_fallback=False) is None
    assert map_sfx_intent("none", glossary) is None
