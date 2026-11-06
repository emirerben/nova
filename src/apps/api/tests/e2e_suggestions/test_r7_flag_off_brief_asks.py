"""R7: with pinned taps off, the brief is read exactly as origin/main reads it.

Failure mode. Round 6 taught ``brief_asks`` that an ``audio`` requirement whose typed facts say a
clip's own sound is on or off (what a mute-clip tap writes) is not an ask about the music or the
sound effects. The narrowing sat in ``_kind_says`` with no flag, so an account with chips on but
pinned taps off (the legacy chips read the same rule) saw the music chip come back over a creator
who had stated a clip-sound ask, where main withholds it. A mute tap is the only writer of such a
requirement and needs the pinned-taps flag; the chat's "clip sound off" is the other, and main
reads it as an `audio` ask. The narrowing now waits for the flag.

  F1  the reader itself: for every requirement shape, flag off, ``spoke`` equals the one main has
      (a verbatim copy of main's ``_kind_says`` is the oracle), for the music and the sound-effects
      dimension; flag on keeps the round-6 reading (the lane is not an ask).
  F2  the real GET, flag off: the chips served over each footage-sound requirement are the chips
      main serves (the oracle patched in for the twin thread), and the music chip is withheld.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefUpdate, apply_updates
from app.services.suggestions import brief_asks
from app.services.suggestions.brief_asks import MUSIC, SFX, spoke
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import build
from tests.e2e_suggestions.test_r6_brief_asks import EN, FOOTAGE_SOUND, TR, audio
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k


def main_kind_says(req, dim):  # noqa: ANN001, ANN201
    """``brief_asks._kind_says`` as origin/main (#2109) has it: no clip-sound lane."""
    if getattr(req, "kind", None) not in dim.kinds:
        return False
    if dim.kind_needs and not brief_asks._description_says(req, dim.kind_needs):
        return False
    return not (dim.kind_not and brief_asks._description_says(req, dim.kind_not))


def ctx_of(update: BriefUpdate) -> SimpleNamespace:
    """A context whose brief holds the requirement ``update`` writes (the brief's own object)."""
    return SimpleNamespace(
        kind_inputs={"brief": apply_updates(None, (update,), source_turn_id=None)}
    )


SHAPES = {
    **FOOTAGE_SOUND,
    "a volume ask": audio("global", "turn the sound down a little"),
    "a sound-effects ask": audio("global", "no sound effects please"),
    "a typed music fact on a toggle": audio(
        "clip:clip-3", "clip sound off", clip_sound="off", music="off"
    ),
    "a clip_sound fact nobody can read": audio("clip:clip-3", "mute it", clip_sound="maybe"),
}


@pytest.mark.parametrize("name", list(SHAPES))
@pytest.mark.parametrize("dim", [MUSIC, SFX], ids=lambda d: d.name)
def test_f1_flag_off_the_reader_is_mains(monkeypatch, name, dim):
    rec = Recorder(f"proof R7-F1 {name} {dim.name}")
    ctx = ctx_of(SHAPES[name])
    with monkeypatch.context() as m:
        m.setattr(brief_asks, "_kind_says", main_kind_says)
        expected = spoke(ctx, dim)
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    rec.negative(
        f"F1 flag off, {name}: {dim.name} is read as main reads it",
        "-",
        f"spoke == {expected}",
        spoke(ctx, dim) is expected,
        f"spoke {spoke(ctx, dim)}",
    )
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    lane = name in FOOTAGE_SOUND
    rec.negative(
        f"F1 flag on, {name}: the footage's own sound lane is not an ask about {dim.name}",
        "-",
        "a clip-sound toggle does not speak; everything else is read as before",
        spoke(ctx, dim) is (False if lane else expected),
        f"spoke {spoke(ctx, dim)}",
    )
    rec.finish()


def chips_of(out) -> list[str]:  # noqa: ANN001
    return [str(c.kind) for c in out.chips]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [EN, TR], ids=["en", "tr"])
@pytest.mark.parametrize("name", list(FOOTAGE_SOUND))
async def test_f2_flag_off_the_get_serves_the_chips_main_serves(
    talking_world, monkeypatch, key, name
):
    rec = Recorder(f"proof R7-F2 {key} {name}")
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    control = chips_of(await build(talking_world, key, EDITOR).phone.serve())
    assert "music" in control, f"the control did not serve the music chip: {control}"
    scene = build(talking_world, key, EDITOR)
    k.write_brief(scene.seeded, FOOTAGE_SOUND[name])
    now = chips_of(await scene.phone.serve())
    with monkeypatch.context() as m:
        m.setattr(brief_asks, "_kind_says", main_kind_says)
        twin = build(talking_world, key, EDITOR)
        k.write_brief(twin.seeded, FOOTAGE_SOUND[name])
        then = chips_of(await twin.phone.serve())
    rec.negative(
        f"F2 flag off, {name}: the chips are the ones main serves",
        key,
        f"{then} (no music chip: main withholds it)",
        now == then and "music" not in now,
        f"now {now}; main {then}",
    )
    rec.finish()
