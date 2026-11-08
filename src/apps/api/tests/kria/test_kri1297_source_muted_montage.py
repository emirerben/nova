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


def _req(rid, **kw) -> BriefRequirement:
    return BriefRequirement(id=rid, kind="audio", scope="global", description=kw.pop("d"), **kw)


def test_targeted_source_muted_is_not_a_whole_video_mute():
    for extra in (
        {"sound_from_s": 2, "sound_to_s": 4},
        {"sound_media": "abc123"},
        {"sound_clip": "last"},
        {"sound_line": "hello"},
        {"sound_except": [{"sound_clip": "last"}]},
    ):
        brief = CreativeBrief(
            requirements=[
                _req("r1", d="Mute my sound", facts={"audio_intent": "source_muted", **extra})
            ]
        )
        assert not brief_mutes_whole_video(brief), extra


def test_later_correction_wins_in_brief_order():
    mute = _req("r7", d="Mute my sound", facts={"audio_intent": "source_muted"})
    on = _req("r99", d="Turn my sound back on", facts={"clip_sound": "on"})
    off = _req("r99", d="Mute my sound", facts={"clip_sound": "off"})
    muted_first = brief_sound_toggle_asks(CreativeBrief(requirements=[mute, on]))
    assert [(a.requirement_id, a.state) for a in muted_first] == [("r7", "off"), ("r99", "on")]
    on_first = brief_sound_toggle_asks(
        CreativeBrief(requirements=[_req("r1", d="Sound on", facts={"clip_sound": "on"}), mute])
    )
    assert [(a.requirement_id, a.state) for a in on_first] == [("r1", "on"), ("r7", "off")]
    assert (
        brief_sound_toggle_asks(CreativeBrief(requirements=[mute, off]))[-1].requirement_id == "r99"
    )


def test_receipt_routes_through_sound_toggle_on_montage_only():
    from app.kria.brief_checks import PlanFacts, check_requirement

    text = "Mute my sound, I'll add a song on TikTok"
    typed = _req("r4", d=text, facts={"audio_intent": "source_muted"})
    twin = _req("r4", d=text, facts={"clip_sound": "off"})
    facts = PlanFacts(phone_montage=True)
    got, want = check_requirement(typed, facts), check_requirement(twin, facts)
    assert (got.status, got.reason) == (want.status, want.reason)
    assert got.status == "met"
    # A voiceover edit keeps the bed reader: it never says "Clip sound off".
    voiceover = check_requirement(typed, PlanFacts(voiceover_edit=True))
    assert voiceover.reason != want.reason
