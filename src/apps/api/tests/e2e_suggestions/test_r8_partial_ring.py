"""R8-B: one card that cannot be rebuilt costs that card, never the others.

The ring entry of a frozen set holds the v2 cards (actions included) a tap and a dismiss resolve.
It was written all-or-nothing: when the entry was missing (the ring keeps 5 sets, so a busy thread
rolls an old one out while Redis still freezes the set) and ONE card no longer built, none were
stored, and the GET fell back to the legacy chips for every card, valid ones included, until the
digest moved or the 24 h freeze lapsed.

Failure catalogue (written first; each row is a test below):

  B1  hook tapped and Undone (the title decision stays in the log, so the hook no longer
      builds), then the set's ring entry is gone. The four other cards are served again, in the
      order they had, on the same set id, and every one executes. Editor triggers on and off,
      brief on and off.
  B2  a title requirement written after the freeze (hook no longer builds), editor triggers off
      (the frozen set outlives it), then the ring entry is gone. Same expectation.
  B3  the ring entry is present (nothing rolled out): the same GET serves the same cards, so the
      partial store changes nothing when every card builds.
  B4  the cards that did rebuild are what the ring holds afterwards: a tap on each one resolves
      (no ``unresolved``), and the withheld hook is not stored.

Every check drives the real path: GET -> worker (cassette) -> GET -> ``submit_turn`` ->
``run_kria_turn`` -> ``_complete_draft_turn``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import sync_session
from app.kria.brief import BriefUpdate
from app.models import CreationThread
from app.services.suggestion_chips import RING_KEY
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

KEY = "nermin-matcha-update"


def drop_entry(seeded, set_id: str) -> None:  # noqa: ANN001
    """The ring has rolled the set's entry out (RING_SIZE) while Redis still freezes the set."""
    with sync_session() as db:
        thread = db.get(CreationThread, seeded.thread_id)
        state = dict(thread.state or {})
        state[RING_KEY] = [e for e in state.get(RING_KEY, []) if e.get("set_id") != set_id]
        thread.state = state
        flag_modified(thread, "state")
        db.commit()


def stored_kinds(seeded, set_id: str) -> list[str]:  # noqa: ANN001
    with sync_session() as db:
        state = db.get(CreationThread, seeded.thread_id).state or {}
    for entry in state.get(RING_KEY, []):
        if entry.get("set_id") == set_id:
            return [c.get("kind") for c in entry.get("suggestions") or []]
    return []


async def tap_all(scene, out) -> list[str]:  # noqa: ANN001
    dead = []
    for card in out.suggestions:
        tap = await scene.phone.tap(card, out)
        if not s.executed(tap):
            dead.append(f"{card.kind}:{(tap.refused or {}).get('reason')}")
        elif tap.new_drafts == 1:
            await scene.phone.undo()
    return dead


@pytest.mark.asyncio
@pytest.mark.parametrize("brief", [True, False], ids=["brief-on", "brief-off"])
@pytest.mark.parametrize("triggers", [True, False], ids=["triggers-on", "triggers-off"])
async def test_b1_a_rolled_out_entry_and_a_spent_hook_cost_the_hook_only(
    talking_world, monkeypatch, triggers, brief
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief)
    scene = s.build(talking_world, KEY, EDITOR)
    first = await scene.phone.serve()
    kinds = s.kinds_of(first)
    assert "hook" in kinds, kinds
    tap = await scene.phone.tap(s.card_of(first, "hook"), first)
    assert s.executed(tap)
    await scene.phone.undo()
    talking_world.redis.advance(60)
    drop_entry(scene.seeded, first.set_id)
    out = await scene.phone.serve()
    want = [kind for kind in kinds if kind != "hook"]
    assert s.kinds_of(out) == want, f"lost cards: {s.kinds_of(out)} vs {want}"
    assert not await tap_all(scene, out)


@pytest.mark.asyncio
async def test_b2_a_title_requirement_and_a_rolled_out_entry_cost_the_hook_only(
    talking_world, monkeypatch
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    scene = s.build(talking_world, KEY, EDITOR)
    first = await scene.phone.serve()
    kinds = s.kinds_of(first)
    k.write_brief(
        scene.seeded,
        BriefUpdate(
            operation="add", kind="text", scope="title", description="a title about tea", facts={}
        ),
    )
    talking_world.redis.advance(60)
    drop_entry(scene.seeded, first.set_id)
    out = await scene.phone.serve()
    assert s.kinds_of(out) == [kind for kind in kinds if kind != "hook"], s.kinds_of(out)
    assert not await tap_all(scene, out)


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", [True, False], ids=["triggers-on", "triggers-off"])
async def test_b3_nothing_rolled_out_serves_what_it_served(talking_world, monkeypatch, triggers):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    scene = s.build(talking_world, KEY, EDITOR)
    first = await scene.phone.serve()
    talking_world.redis.advance(60)
    again = await scene.phone.serve()
    assert s.kinds_of(again) == s.kinds_of(first)
    assert again.set_id == first.set_id


@pytest.mark.asyncio
async def test_b4_the_ring_holds_the_cards_that_rebuilt_and_not_the_one_that_did_not(
    talking_world, monkeypatch
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    scene = s.build(talking_world, KEY, EDITOR)
    first = await scene.phone.serve()
    kinds = s.kinds_of(first)
    k.write_brief(
        scene.seeded,
        BriefUpdate(
            operation="add", kind="text", scope="title", description="a title about tea", facts={}
        ),
    )
    talking_world.redis.advance(60)
    drop_entry(scene.seeded, first.set_id)
    out = await scene.phone.serve()
    assert out.set_id == first.set_id
    assert stored_kinds(scene.seeded, out.set_id) == [kind for kind in kinds if kind != "hook"]
