"""Undo gives the card back exactly once.

Failure catalogue (written first; each row is a line of "Negative cases" in the report):

  U1  Undo leaves the card spent. The creator taps a card, does not like it, Undoes, and the card
      on the phone (the same set) is dead: "no longer fits". The documented contract is that Undo
      clears the card's applied mark so it is an offer again. The one exception the docs name is a
      hook: its creative-copy decision stays in the event log, so the hook card stays spent.
      Recorded as observed, in both directions.
  U2  Undo gives the card back TWICE. After Undo and a second tap, a third tap must be refused
      ("consumed"): the card is one offer per Undo, never a stack of the same change.
  U3  the second application is a different change. The re-tap rebuilds the same change as the
      first on an edit that is the parent again (same lane, same direction), not a fresh step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.flow import LANE, RESTATE
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"
KINDS = [
    ("captions", EDITOR),
    ("music", EDITOR),
    ("transition", EDITOR),
    ("sfx", EDITOR),
    ("hook", EDITOR),
    ("duplicates", SIGNALS),
    ("filler_words", TALKING),
]


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "profile"), KINDS)
async def test_u1_undo_gives_the_card_back_once(talking_world, kind, profile):
    rec = Recorder(f"negative U {kind}")
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, talking_world, prefs(KEY), language=scn.language)
    out = await phone.serve()
    card = next(c for c in out.suggestions if c.kind == kind)

    first = await phone.tap(card, out)
    await phone.undo()
    second = await phone.tap(card, out)
    third = await phone.tap(card, out)

    if kind == "hook":
        # The documented limit: a creative-copy decision stays in the event log after Undo.
        rec.negative(
            "hook: after Undo the card stays spent (the decision is in the event log)",
            KEY,
            "the second tap is refused in words, nothing written",
            (second.refused or {}).get("outcome") == "refused" and second.new_drafts == 0,
            f"signal={second.refused} planned={second.planned}",
        )
    else:
        same_change = _same_change(kind, first, second)
        rec.negative(
            f"{kind}: Undo gives the card back; the second tap applies the same change once",
            KEY,
            "planned, one new revision, the same lane and direction as the first tap",
            second.planned and second.new_drafts == 1 and same_change,
            f"pin={second.pin} drafts +{second.new_drafts} same change: {same_change} "
            f"reply={second.reply[:50]!r}",
        )
        quiet = third.new_drafts == 0 and third.before == third.after
        typed = (third.refused or {}).get("reason") == "consumed"
        rec.negative(
            f"{kind}: ...and a third tap is refused (one offer per Undo, never a stack)",
            KEY,
            "nothing written; refused consumed with the typed signal",
            quiet and (typed or kind in RESTATE),
            f"signal={third.refused} drafts +{third.new_drafts} reply={third.reply[:50]!r}",
        )
        if kind in RESTATE:
            # A restating card's approval is pending, so the duplicate-send guard answers at
            # submit: quiet, and it carries the same typed refusal as every other second tap.
            rec.negative(
                f"{kind}: ...with the typed refusal signal",
                KEY,
                "refused consumed",
                typed,
                f"signal={third.refused}",
            )
    rec.finish()


def _same_change(kind: str, first: d.Tap, second: d.Tap) -> bool:
    if kind in RESTATE:
        return second.result.get("status") == first.result.get("status") == "awaiting_approval"
    lane = LANE[kind]
    a = _without_ids(d.lane_diff(first.head_before, first.head_after).get(lane))
    b = _without_ids(d.lane_diff(second.head_before, second.head_after).get(lane))
    return a is not None and a == b


def _without_ids(node):  # noqa: ANN001, ANN202
    """A sound effect entry is minted with a fresh id each time; the change is everything else."""
    if isinstance(node, dict):
        return {k: _without_ids(v) for k, v in node.items() if k != "id"}
    if isinstance(node, list | tuple):
        return [_without_ids(v) for v in node]
    return node
