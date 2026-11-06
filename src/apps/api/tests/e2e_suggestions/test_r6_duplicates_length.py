"""R6 defect 2 (KRI-129): the duplicates card keeps the video's length the creator stated.

Failure-mode list (written before the code; each row is a test below).

The card removes the weaker of two near-identical takes. ``needed_media`` named the clips a request
points at, and not the video's length, so a creator who asked for "6 seconds" was still served
"keep the better take", and its tap shortened the video (6 s to 4 s, "Also changed: Video length")
with the creator's own ask never re-judged on an editor-ops tap.

  L1  a stated length (exact, or a ceiling) withholds the card on the real GET. The control, the
      same thread without the ask, serves it.
  L2  the ask arrives AFTER the card was served: the tap is refused and writes nothing, never a
      shorter video.
  L3  only a typed length counts: a timing ask with no duration fact (pace) leaves the card, in
      any wording. The words are not read.

Every check drives the real path: GET -> ring -> ``submit_turn`` -> ``run_kria_turn``.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import build, card_of, executes, kinds_of
from tests.e2e_suggestions.threads import SIGNALS
from tests.tasks import kria_pinned_kinds_harness as k

KEY = "nermin-matcha-update"

LENGTHS = {
    "exact": dict(duration_s=6),
    "ceiling": dict(duration_s=6, duration_bound="at_most"),
}


def length_ask(**facts) -> BriefUpdate:  # noqa: ANN003
    return BriefUpdate(
        operation="add", kind="timing", scope="global", description="6 seconds", facts=facts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LENGTHS)
async def test_l1_a_stated_length_withholds_the_duplicates_card(talking_world, name):
    rec = Recorder(f"R6 duplicates length L1 {name}")
    control = build(talking_world, KEY, SIGNALS)
    assert "duplicates" in kinds_of(await control.phone.serve()), "the control must serve it"

    scene = build(talking_world, KEY, SIGNALS)
    k.write_brief(scene.seeded, length_ask(**LENGTHS[name]))
    served = kinds_of(await scene.phone.serve())
    rec.negative(
        f"L1 the creator asked for a {name} length: the duplicates card is withheld",
        KEY,
        "duplicates absent",
        "duplicates" not in served,
        f"served {served}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_l2_a_length_stated_after_the_card_was_served_refuses_the_tap(talking_world):
    rec = Recorder("R6 duplicates length L2")
    scene = build(talking_world, KEY, SIGNALS)
    out = await scene.phone.serve()
    card = card_of(out, "duplicates")
    assert card is not None, "the control must serve it"
    k.write_brief(scene.seeded, length_ask(duration_s=6))
    tap = await scene.phone.tap(card, out)
    rec.negative(
        "L2 the length arrived after the serve: the tap is refused, no draft",
        KEY,
        "refused, no new draft",
        not executes(tap) and tap.new_drafts == 0,
        f"refused={tap.refused} reply={tap.reply[:80]!r} drafts {tap.before} -> {tap.after}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["keep the pace fast", "hızlı kesimler olsun"])
async def test_l3_a_timing_ask_with_no_length_leaves_the_card(talking_world, text):
    rec = Recorder(f"R6 duplicates length L3 {text}")
    scene = build(talking_world, KEY, SIGNALS)
    k.write_brief(
        scene.seeded,
        BriefUpdate(operation="add", kind="timing", scope="global", description=text, facts={}),
    )
    served = kinds_of(await scene.phone.serve())
    rec.negative(
        f"L3 {text!r} states no length: the duplicates card stays",
        KEY,
        "duplicates served",
        "duplicates" in served,
        f"served {served}",
    )
    rec.finish()
