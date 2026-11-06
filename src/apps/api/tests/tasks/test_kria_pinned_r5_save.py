"""R5 defect 6 (GAP-12): the session's pointer after an editor tap and its Undo.

Failure-mode list (written before the code; each row is a fixture below).

A restating card (filler, mute) re-drafts the thread's PLAN, and its approval mints a Job from that
plan, so it may only be offered, and may only run, while the plan holds what the saved video
holds. The witness is the agent session's pointer (``session.target_generation_id``, "the video
the agent last read") against the saved variant (``pinned.save_unread``). A native Save moves the
variant and never the pointer; an editor-op draft is BUILT on the saved video, so committing one
re-syncs the pointer to it. Undo then makes the head the PLAN again but left the pointer on the
saved video, which lifted the fence over a plan that holds none of the Save.

  P1  Save -> an editor card -> Undo: the pointer is back where the tap found it, the filler card
      is not served, and the card the phone still holds is refused ``base_moved`` with nothing
      written. (This is the data-loss sequence: accepted, approving it renders without the Save.)
  P2  No Save, an editor card, Undo: the tap re-synced nothing, so Undo moves nothing and the
      filler card is offered and runs. The control for P1: the fence is not just "after any Undo".
  P3  Two editor cards on top of one Save: undoing the newer one keeps the pointer on the saved
      video (the older editor draft is still the head and was built on it); undoing the older one
      puts it back. The pointer follows the draft, not the Undo count.
  P4  The agent reads the saved video after the Undo (a Kria render completes): the pointer follows
      that render and the filler card returns. Undo never pulls a pointer back that something else
      moved after the tap (a restore only undoes the tap's own sync).
  P5  The same flow with the pinned-taps switch off writes no footprint at all: the execution row is
      byte-identical to the one the branch's base wrote, and Undo leaves the pointer alone.
  P6  A session with no pointer yet (nothing read): the tap's sync and the Undo's restore both
      round-trip the empty value, the card stays offered.

The Save is the real ``POST /plan-items/{id}/variants/{id}/editor-commit`` (through the ASGI app);
the render worker's completion write is the one stand-in. Undo is ``undo_draft``, the function the
route calls.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.database import sync_session
from app.models import CreatorAgentExecution, CreatorAgentSession
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_r4_save import (
    agent_reads_video,
    card_of,
    pointer,
    route_save,
    served,
    untouched,
)

_dispose_async_engine = e2e_world._dispose_async_engine
world = e2e_world.world
talking_world = e2e_world.talking_world


def set_pointer(seeded, value) -> None:  # noqa: ANN001
    with sync_session() as db:
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = value
        db.commit()


def kinds(out) -> set[str]:  # noqa: ANN001
    return {c.kind for c in out.suggestions}


def head_kind(seeded) -> str | None:  # noqa: ANN001
    head = d.head_of(seeded.thread_id)
    return ((head or {}).get("snap") or {}).get("kind")


def pointer_records(seeded) -> list[dict]:  # noqa: ANN001
    """The pointer footprints the editor-op drafts wrote, oldest first."""
    from sqlalchemy import select

    with sync_session() as db:
        rows = db.execute(
            select(CreatorAgentExecution.result)
            .where(CreatorAgentExecution.session_id == seeded.session_id)
            .order_by(CreatorAgentExecution.created_at)
        ).scalars()
        return [
            dict(r["pointer_sync"]) for r in rows if isinstance(r, dict) and "pointer_sync" in r
        ]


@pytest.mark.asyncio
async def test_p1_save_editor_tap_undo_keeps_the_fence_up(talking_world):
    seeded, phone, first = await served(talking_world)
    held = card_of(first, "filler_words")
    before = pointer(seeded)
    generation = await route_save(seeded)
    assert generation != before
    talking_world.refreshes.clear()

    out = await phone.serve()
    tapped = await phone.tap(card_of(out, "captions"), out)
    assert tapped.refused is None and tapped.new_drafts == 1, tapped.refused
    assert pointer(seeded) == generation, "an editor draft is built on the saved video"
    assert head_kind(seeded) == "editor"

    await phone.undo()

    assert head_kind(seeded) == "strategy", "Undo makes the plan the head again"
    assert pointer(seeded) == before, "the pointer goes back to the video the agent had read"
    talking_world.refreshes.clear()
    after = await phone.serve()
    assert "filler_words" not in kinds(after), kinds(after)
    assert kinds(after), "the cards that do not restate the plan are still offered"

    tap = await phone.tap(held, first)
    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)
    assert not d.item_flags(seeded)["speech_cleanup_enabled"]
    assert head_kind(seeded) == "strategy"


@pytest.mark.asyncio
async def test_p2_without_a_save_the_undo_changes_nothing(talking_world):
    seeded, phone, first = await served(talking_world)
    before = pointer(seeded)
    out = await phone.serve()
    tapped = await phone.tap(card_of(out, "captions"), out)
    assert tapped.refused is None and tapped.new_drafts == 1, tapped.refused
    assert pointer_records(seeded) == [], "the pointer already matched: nothing to put back"

    await phone.undo()

    assert pointer(seeded) == before
    talking_world.refreshes.clear()
    after = await phone.serve()
    card = card_of(after, "filler_words")
    tap = await phone.tap(card, after)
    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert d.item_flags(seeded)["speech_cleanup_enabled"]


@pytest.mark.asyncio
async def test_p3_the_pointer_follows_the_draft_not_the_undo_count(talking_world):
    seeded, phone, first = await served(talking_world)
    generation = await route_save(seeded)
    talking_world.refreshes.clear()
    out = await phone.serve()
    await phone.tap(card_of(out, "captions"), out)
    out = await phone.serve()
    second = await phone.tap(card_of(out, "music"), out)
    assert second.refused is None and second.new_drafts == 1, second.refused
    assert len(pointer_records(seeded)) == 1, "only the tap that moved the pointer records it"

    await phone.undo()

    assert head_kind(seeded) == "editor", "the captions draft is still the head"
    assert pointer(seeded) == generation, "and it was built on the saved video"
    talking_world.refreshes.clear()
    assert "filler_words" not in kinds(await phone.serve()), "an editor head still holds the card"


@pytest.mark.asyncio
async def test_p4_a_later_read_of_the_video_is_not_pulled_back(talking_world):
    seeded, phone, first = await served(talking_world)
    before = pointer(seeded)
    generation = await route_save(seeded)
    talking_world.refreshes.clear()
    out = await phone.serve()
    await phone.tap(card_of(out, "captions"), out)
    assert pointer(seeded) == generation
    # Something else (a render's completion) moved the pointer after the tap.
    set_pointer(seeded, "gen-written-after-the-tap")

    await phone.undo()

    assert pointer(seeded) == "gen-written-after-the-tap"

    # And the ordinary way back: the agent reads the saved video, the card returns and runs.
    agent_reads_video(seeded)
    talking_world.refreshes.clear()
    after = await phone.serve()
    tap = await phone.tap(card_of(after, "filler_words"), after)
    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert before != generation


@pytest.mark.asyncio
async def test_p5_switch_off_at_commit_writes_no_footprint(talking_world, monkeypatch):
    seeded, phone, first = await served(talking_world)
    generation = await route_save(seeded)
    talking_world.refreshes.clear()
    out = await phone.serve()
    # The master switch goes off between planning and the row-locked commit.
    h.after_planning(
        monkeypatch,
        lambda: monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False),
    )
    tapped = await phone.tap(card_of(out, "captions"), out)
    assert tapped.refused is None and tapped.new_drafts == 1, tapped.refused
    assert pointer(seeded) == generation, "the draft is built on the saved video as before"
    assert pointer_records(seeded) == [], "off: the execution row is what the base wrote"

    await phone.undo()

    assert pointer(seeded) == generation, "off: Undo is what the base did"


@pytest.mark.asyncio
async def test_p6_a_session_with_no_pointer_round_trips_the_empty_value(talking_world):
    seeded, phone, first = await served(talking_world)
    set_pointer(seeded, None)
    generation = await route_save(seeded)
    talking_world.refreshes.clear()
    out = await phone.serve()
    tapped = await phone.tap(card_of(out, "captions"), out)
    assert tapped.refused is None and tapped.new_drafts == 1, tapped.refused
    assert pointer(seeded) == generation

    await phone.undo()

    assert pointer(seeded) is None
    talking_world.refreshes.clear()
    after = await phone.serve()
    assert "filler_words" in kinds(after), "nothing has been read: nothing to compare"
