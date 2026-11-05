"""Every card the server serves, tapped through the real pinned path, on the replay fixtures.

Failure catalogue (written before the driver; each row is a check on a cell of ``.chip-eval/
e2e-report.md``). The rule: a creator who taps a served card gets exactly the change the card
promised, visibly and undoably, or is told plainly that it no longer fits. Never a silent no-op,
never a second change, never the planner's free reading of the card's words.

  E1  a served card whose tap does nothing. Served on the real GET, tapped with the card's prompt
      and ``{set_id, chip_id}``: the turn is accepted and PLANNED (never refused), and its change is
      real: a new head draft whose parent is the previous head and whose diff sits in the lane the
      card promised (filler: the item setting flipped plus the staged render approval; mute: the
      typed ``clip_sound`` requirement plus the approval).
  E2  the chat line is not the card's prompt (the planner re-worded it) or a hook's copy leaked into
      the creator's message (the ``creator_quoted`` bypass). The line equals ``card.prompt`` and
      never contains the hook text.
  E3  a "done" with no receipt. The turn's requirement receipts exist and are met.
  E4  Undo does not bring the parent back. The head after Undo equals the head before the tap
      and no approval is left pending.
  E5  a handled card comes back or stacks. After one tap the ring marks it consumed; a second tap on
      the same card is refused in words ("no longer fits"), with the typed ``suggestion_pin``
      signal, writes no row, asks for fresh cards (``pinned_stale``); the next read is a NEW
      set without the card while the other cards stay tappable.
  E6  the server serves a card its own evidence does not support, or hides one it does. The served
      kinds are exactly what the measured signals, the brief and the cassette order give; a clip the
      creator's request names is never in a duplicates card and a recorded title requirement keeps
      the hook card out (KRI-129).
  E7  the v2 cards reach the ring the phone's tap is resolved against. A tap on a card the ring does
      not hold answers "no longer fits" however good the card was.

Evidence is the whole pipeline: the real GET -> the real ``kria_suggester`` body with the real
``SuggestionAgent`` (a cassette answers) -> ``submit_turn`` -> ``run_kria_turn`` on Postgres.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.flow import GAP_RESTATE_NOT_READY, tap_every_card
from tests.e2e_suggestions.gaps import Gap
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

CASSETTES = Path(__file__).resolve().parent / "cassettes"


def preferences(scenario_key: str) -> list[dict]:
    name = scenario_key.replace("/", "_")
    return json.loads((CASSETTES / f"{name}.json").read_text(encoding="utf-8"))["prefs"]


# What each thread must serve, in rank order, with every v2 flag on and the mute kind dark (as
# shipped). Each list is the evidence and the brief at work, not a snapshot of an output:
#
# * ``editor``   a voice, no title, captions off, hard cuts, a music track: hook, captions, music,
#                transition, sfx. The east run's recorded brief has a title (hook out).
# * ``signals``  two takes of one shot and a clip with wind: duplicates is served, mute is not (dark
#                constant). The east run's capture-time order names every clip (duplicates out).
# * ``talking``  one take with a measured cleanup analysis: filler words. The east run's title
#                requirement keeps the hook out.
EXPECT: dict[tuple[str, str], list[str]] = {
    ("nermin-matcha-update", EDITOR): ["hook", "captions", "music", "transition", "sfx"],
    ("nermin-matcha-update", SIGNALS): ["duplicates", "hook", "captions", "sfx"],
    ("nermin-matcha-update", TALKING): ["filler_words", "hook", "captions", "music", "sfx"],
    ("nermin-travel-diary", EDITOR): ["hook", "captions", "music", "transition", "sfx"],
    ("nermin-travel-diary", SIGNALS): ["duplicates", "hook", "captions", "sfx"],
    ("east-run-brief", EDITOR): ["captions", "music", "transition", "sfx"],
    ("east-run-brief", SIGNALS): ["captions", "transition", "sfx"],
    ("east-run-brief", TALKING): ["filler_words", "captions", "music", "sfx"],
    ("east-run-brief/footage-only", EDITOR): ["hook", "captions", "music", "transition"],
    ("east-run-brief/footage-only", SIGNALS): ["duplicates", "hook", "captions", "sfx"],
}


# Cards the server serves today whose tap is refused with a typed reason: a named gap, not a pass.
# The east run's recorded plan has no per-clip text lane (``plan_shape.has_per_clip_text_lane`` is
# false) for its landmark-label request, so a plan restate cannot be approved as it stands.
REFUSED_AT_TAP: dict[tuple[str, str], dict[str, tuple[str, Gap]]] = {
    ("east-run-brief", TALKING): {
        "filler_words": ("requirements_not_ready", GAP_RESTATE_NOT_READY)
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "profile"), list(EXPECT), ids=[f"{k}:{p}" for k, p in EXPECT])
async def test_tap_every_served_card(talking_world, key, profile):
    scn = threads.SCENARIOS[key]
    rec = Recorder(f"tap-every-card {key} {profile}")
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, talking_world, preferences(key), language=scn.language)
    await tap_every_card(
        rec,
        phone,
        scn,
        profile,
        expect=EXPECT[(key, profile)],
        refusals=REFUSED_AT_TAP.get((key, profile)),
    )
    rec.finish()
