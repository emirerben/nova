"""R4-E5: no switch works behind another one. The editor-triggers refresh needs the chip capability.

Failure catalogue (written first):

  E5  ``refresh_for_item`` (called after every editor Save and every completed turn) read the
      database and wrote a Redis debounce key for each thread before the enqueue finally refused
      because chips were off. ``config.py`` says every new flag is subordinate to the chip
      capability ("with CHIPS_ENABLED off nothing below does anything"). With chips off, for
      everyone or for this creator, a Save must do nothing at all: no lookup, no key, no run.
  E5b the guard is too wide: with chips on for the creator the Save still takes its slot and
      enqueues exactly one run.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.services import suggestion_serve
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"


async def served(world):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    prefs = json.loads((CASSETTES / f"{KEY}.json").read_text(encoding="utf-8"))["prefs"]
    phone = Phone(seeded, world, prefs, language=scn.language)
    await phone.serve()
    world.refreshes.clear()
    world.redis.data.clear()
    return seeded


def debounce_keys(world) -> list[str]:  # noqa: ANN001
    return [key for key in world.redis.data if "debounce" in key]


@pytest.mark.asyncio
async def test_e5_refresh_for_item_does_nothing_with_chips_off(talking_world, monkeypatch):
    rec = Recorder("negative E5 refresh with chips off")
    seeded = await served(talking_world)
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])
    reads: list[object] = []
    real = suggestion_serve._active_threads
    monkeypatch.setattr(
        suggestion_serve, "_active_threads", lambda item_id: reads.append(item_id) or real(item_id)
    )
    found = suggestion_serve.refresh_for_item(seeded.item_id, trigger="editor_save")
    rec.negative(
        "chips off for everyone: a Save reads nothing, writes no debounce key, enqueues nothing",
        KEY,
        "0 enqueued, no thread lookup, no Redis key",
        found == 0
        and not reads
        and not debounce_keys(talking_world)
        and not talking_world.refreshes,
        f"found={found} reads={len(reads)} keys={debounce_keys(talking_world)} "
        f"enqueued={talking_world.refreshes}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_e5_a_creator_outside_the_allowlist_gets_no_debounce_slot(talking_world, monkeypatch):
    rec = Recorder("negative E5 refresh for a creator outside the chip allowlist")
    seeded = await served(talking_world)
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [str(uuid.uuid4())])
    found = suggestion_serve.refresh_for_item(seeded.item_id, trigger="editor_save")
    rec.negative(
        "chips on for other accounts only: this creator's Save writes no debounce key",
        KEY,
        "0 enqueued, no Redis key",
        found == 0 and not debounce_keys(talking_world) and not talking_world.refreshes,
        f"found={found} keys={debounce_keys(talking_world)} enqueued={talking_world.refreshes}",
    )
    # The control: the same Save for an allowlisted creator takes a slot and enqueues one run.
    talking_world.redis.data.clear()
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [str(seeded.user_id)])
    found = suggestion_serve.refresh_for_item(seeded.item_id, trigger="editor_save")
    rec.negative(
        "chips on for this creator: the Save takes a debounce slot and enqueues one run",
        KEY,
        "1 enqueued, one debounce key",
        found == 1 and len(debounce_keys(talking_world)) == 1 and len(talking_world.refreshes) == 1,
        f"found={found} keys={debounce_keys(talking_world)} enqueued={talking_world.refreshes}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_e5_the_control_chips_on_refresh_still_runs(talking_world):
    rec = Recorder("negative E5 control: refresh with chips on")
    seeded = await served(talking_world)
    found = suggestion_serve.refresh_for_item(seeded.item_id, trigger="editor_save")
    rec.negative(
        "chips on: a Save refreshes the thread (the guard did not turn the feature off)",
        KEY,
        "1 enqueued, one debounce key",
        found == 1 and len(debounce_keys(talking_world)) == 1 and h.world_refreshes(talking_world),
        f"found={found} keys={debounce_keys(talking_world)} enqueued={talking_world.refreshes}",
    )
    rec.finish()
