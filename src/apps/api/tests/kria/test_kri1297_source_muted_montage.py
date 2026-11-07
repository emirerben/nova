"""KRI-1297: "Mute my sound" typed as ``source_muted`` is a whole-video mute on a montage.

Failure modes (written before the fix):

F1  The brief writer files "Mute my sound, I'll add a song on TikTok" as
    ``audio_intent: source_muted``; only the voiceover bed reads it, so a
    montage kept the clip sound.
F2  "Keep the crowd low" mis-tagged ``source_muted`` must not mute (stays the bed reader's).
F3  A clip-scoped / non-audio / retracted requirement must not become a whole-video mute.
F4  A "play it louder" or "keep" ask mis-tagged ``source_muted`` must not mute.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.clip_sound_toggle_ask import (
    SOUND_OFF,
    brief_mutes_whole_video,
    brief_sound_toggle_asks,
)


def _brief(text: str, *, kind="audio", scope="global", facts=None) -> CreativeBrief:
    req = BriefRequirement(
        id="r4",
        kind=kind,
        scope=scope,
        description=text,
        facts={"audio_intent": "source_muted"} if facts is None else facts,
    )
    return CreativeBrief(requirements=[req])


def test_mute_my_sound_is_a_whole_video_mute():
    brief = _brief("Mute my sound, I'll add a song on TikTok")
    (ask,) = brief_sound_toggle_asks(brief)
    assert ask.state == SOUND_OFF and ask.target.whole_video
    assert brief_mutes_whole_video(brief)


def test_turkish_mute():
    assert brief_mutes_whole_video(_brief("Sesimi kapat, TikTok'ta şarkı ekleyeceğim"))


def test_not_a_mute():
    for text in (
        "Keep the crowd noise low under my voice",
        "Keep my sound",
        "Make my sound louder",
        "Lower my sound",
    ):
        assert not brief_mutes_whole_video(_brief(text)), text


def test_other_shapes_untouched():
    assert not brief_mutes_whole_video(_brief("Mute my sound", scope="clip:abc123"))
    assert not brief_mutes_whole_video(_brief("Mute my sound", kind="style"))
    assert not brief_mutes_whole_video(_brief("Mute my sound", facts={}))
