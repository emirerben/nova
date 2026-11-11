"""R6 defect 3: an `audio` requirement about a clip's own sound does not speak for the music card.

Failure-mode list (written before the code; each row is a test below).

``brief_asks`` reads the creator's word from typed data (KRI-667): the brief writer files every
volume, music and sound ask as an ``audio`` requirement, so any live ``audio`` requirement
withheld the music card. That also withheld it over a requirement about something else on the
audio kind: the footage's own sound turned on or off, which is exactly what a mute-clip tap
writes (``audio``, scope ``clip:<id>``, ``clip_sound: off``) and what "clip sound off except that
last line" is. The typed signal that lane has (``clip_sound``, read by ``clip_sound_toggle_ask``)
now places it.

  B1  the requirement a mute-clip tap writes leaves the music and sound-effects cards on offer.
  B2  so does the whole-video, with-exceptions form the brief writer records for "clip sound off
      except that last line".
  B3  GAP-15 stays closed: an `audio` requirement with no such fact withholds the music card in
      any wording and either language, as before.
  B4  a `clip_sound` fact the reader cannot parse is NOT the footage lane (fail closed): the
      requirement still speaks.
  B5  a toggle that also carries a typed music fact still speaks for music (the fact decides).

Every check drives the real GET.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import build, kinds_of
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

TR = "nermin-matcha-update/tr"
EN = "nermin-matcha-update"


def audio(scope: str, text: str | None = None, **facts) -> BriefUpdate:  # noqa: ANN003
    return BriefUpdate(
        operation="add", kind="audio", scope=scope, description=text, facts=facts or {}
    )


async def served(world, key: str, *updates: BriefUpdate) -> list[str]:  # noqa: ANN001
    scene = build(world, key, EDITOR)
    if updates:
        k.write_brief(scene.seeded, *updates)
    return kinds_of(await scene.phone.serve())


FOOTAGE_SOUND = {
    # What the mute-clip card's tap writes (`mute_clip._build`).
    "mute tap": audio("clip:clip-3", "Mute the sound of clip 3", clip_sound="off"),
    # "Clip sound off except that last line" (KRI-942).
    "whole video with an exception": audio(
        "global",
        "clip sound off except the last line",
        clip_sound="off",
        sound_except=[{"sound_clip": "last"}],
    ),
    "sound back on for a clip": audio("clip:clip-1", "keep clip 1's own sound", clip_sound="on"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [EN, TR])
@pytest.mark.parametrize("name", list(FOOTAGE_SOUND))
async def test_b1_b2_a_clip_sound_ask_leaves_the_music_and_sfx_cards(talking_world, key, name):
    rec = Recorder(f"R6 brief asks B1/B2 {key} {name}")
    control = await served(talking_world, key)
    assert "music" in control, f"the control did not serve the music card: {control}"
    now = await served(talking_world, key, FOOTAGE_SOUND[name])
    rec.negative(
        f"B1/B2 {name}: the music and sound-effects cards are not withheld",
        key,
        "the served cards equal the control's",
        now == control,
        f"without {control}; with {now}",
    )
    rec.finish()


SOUNDTRACK_ASKS = [("sesi biraz kıs", TR), ("melodiyi değiştir", TR), ("tone down the sound", EN)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "key"), SOUNDTRACK_ASKS, ids=[t for t, _ in SOUNDTRACK_ASKS])
async def test_b3_a_soundtrack_ask_with_no_fact_still_withholds_the_music_card(
    talking_world, text, key
):
    rec = Recorder(f"R6 brief asks B3 {text}")
    control = await served(talking_world, key)
    now = await served(talking_world, key, audio("global", text))
    rec.negative(
        f"B3 {text!r} (no typed fact): the music card is withheld and only it",
        key,
        "music absent, the other cards untouched",
        "music" not in now and [x for x in control if x != "music"] == now,
        f"without {control}; with {now}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_b4_b5_what_the_footage_reader_cannot_place_still_speaks(talking_world):
    rec = Recorder("R6 brief asks B4/B5")
    control = await served(talking_world, EN)
    unreadable = await served(
        talking_world,
        EN,
        audio("clip:clip-3", "mute clip 3", clip_sound="off", sound_clip="middle"),
    )
    rec.negative(
        "B4 a clip_sound fact the reader cannot parse: the music card is withheld",
        EN,
        "music absent",
        "music" in control and "music" not in unreadable,
        f"without {control}; with {unreadable}",
    )
    both = await served(
        talking_world,
        EN,
        audio("global", "mute it and lower the music", clip_sound="off", music_level="low"),
    )
    rec.negative(
        "B5 a toggle that also carries a typed music fact: the music card is withheld",
        EN,
        "music absent",
        "music" not in both,
        f"with {both}",
    )
    rec.finish()
