"""The refusal mark and the typed signal are written only when the refusal is the turn's outcome.

Failure catalogue (written before the code; each row is a fixture below).

  H1  A consent refusal marks the card refused on its ring entry BEFORE the reply transaction
      checks the brief version. When the brief moved after planning, that check requeues the turn
      and commits the dirty ring: the card is already hidden and a new set is minted for a turn
      whose outcome is not decided yet. If the replay then applies, or refuses for a transient
      reason, the ring carries a mark the turn never earned. Expected: a requeued turn leaves the
      ring exactly as it was; the replay's own outcome writes its own mark.
  H2  A second tap on a filler (or mute) card while its approval is pending is answered by the
      duplicate-send guard at submit: nothing is written, but there is no typed ``suggestion_pin``
      record, no refresh for fresh cards and no refusal mark, so the phone's ledger never lets go.
      Expected: the same record every other refusal carries (reason ``consumed``) on the reply and
      the turn's plan, ``suggestion_outcome: refused`` on the ack and on a replay of it, the card
      marked refused (the next read mints a new set without it) and a ``pinned_stale`` refresh.
  H3  Control: a duplicate of a message the creator TYPED (no tapped card behind it) is answered
      exactly as before: no record, no mark, no refresh.
  H4  Control: with the pinned-taps switch off the tap is not stamped, so a repeat is answered as
      before (no record, no mark, no refresh).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import TALKING
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_kinds import (  # noqa: F401 - the guard runs on import
    _PROBLEM,
    HOOK_TEXT,
    add_creator_title,
    tap_and_run,
)

_dispose_async_engine = e2e_world._dispose_async_engine
world = e2e_world.world
talking_world = e2e_world.talking_world

CASSETTES = Path(__file__).resolve().parents[1] / "e2e_suggestions" / "cassettes"
KEY = "nermin-matcha-update"


def prefs() -> list[dict]:
    path = CASSETTES / f"{KEY}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def served(world):  # noqa: ANN001, ANN201
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, TALKING)
    threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


def filler(out):  # noqa: ANN001, ANN201
    return next(c for c in out.suggestions if c.kind == "filler_words")


@pytest.mark.asyncio
async def test_h1_a_requeued_refusal_leaves_the_ring_as_it_was(world, monkeypatch):
    from app.kria.brief import BriefUpdate

    k.install_inputs(monkeypatch, stale_copy_states=True)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("sfx", rank=2))
    add_creator_title(seeded, await h.media_digest_of(seeded))
    moved = []

    def brief_moves_once() -> None:
        if not moved:  # the replay plans against the new version and must not move it again
            moved.append(True)
            k.write_brief(
                seeded,
                BriefUpdate(
                    operation="add", kind="style", scope="global", description="A warmer look"
                ),
            )

    h.after_planning(monkeypatch, brief_moves_once)
    card = seeded.cards["hook"]

    accepted, first = await tap_and_run(seeded, "hook")

    assert first["status"] == "requeued", first
    assert "refused" not in k.ring_entry(seeded.thread_id, seeded.set_id)

    replay = await h.run(accepted.turn_id)

    assert replay["status"] == "completed", replay
    entry = k.ring_entry(seeded.thread_id, seeded.set_id)
    assert entry["refused"] == {card.id: accepted.turn_id}, "the replay's own outcome marks it"


@pytest.mark.asyncio
async def test_h2_a_second_tap_on_a_pending_restate_card_is_a_typed_refusal(talking_world):
    seeded, phone, out = await served(talking_world)
    card = filler(out)
    first = await phone.tap(card, out)
    assert first.planned and first.result["status"] == "awaiting_approval"
    talking_world.refreshes.clear()

    again = await phone.tap(card, out)

    record = {"set_id": out.set_id, "id": card.id, "outcome": "refused", "reason": "consumed"}
    assert again.refused == record, again.refused
    assert again.pin == record, "the turn's plan carries it too, so a replayed submit can read it"
    assert again.accepted.suggestion_outcome == "refused"
    assert again.new_drafts == 0 and again.before == again.after
    assert "pinned_stale" in h.world_refreshes(talking_world)
    entry = phone.ring_entry(out.set_id)
    assert entry["refused"] == {card.id: again.accepted.turn_id}
    assert card.id not in {c.id for c in (await phone.get()).suggestions}


@pytest.mark.asyncio
async def test_h2_a_client_retry_of_the_second_tap_reads_refused_off_the_plan(talking_world):
    seeded, phone, out = await served(talking_world)
    card = filler(out)
    await phone.tap(card, out)
    tap = {"set_id": out.set_id, "chip_id": card.id}
    asked_at = h.revision(seeded.thread_id)
    again, _publish = await h.submit(seeded, card.prompt, tap=tap, expected_revision=asked_at)

    replay, _publish = await h.submit(
        seeded,
        card.prompt,
        tap=tap,
        event_id=h.turn_row(again.turn_id).client_event_id,
        expected_revision=asked_at,
    )

    assert replay.replayed and replay.suggestion_outcome == "refused"


@pytest.mark.asyncio
async def test_h3_a_duplicate_of_a_typed_message_is_answered_as_before(talking_world):
    seeded, phone, out = await served(talking_world)
    await h.submit(seeded, "Make the intro shorter")
    talking_world.refreshes.clear()

    accepted, _publish = await h.submit(seeded, "Make the intro shorter")

    reply = [e for e in h.events(seeded.thread_id) if e.role == "assistant"][-1]
    assert accepted.status == "completed" and accepted.suggestion_outcome is None
    assert "suggestion_pin" not in (reply.payload or {})
    assert "pinned_stale" not in h.world_refreshes(talking_world)
    assert "refused" not in (phone.ring_entry(out.set_id) or {})


@pytest.mark.asyncio
async def test_h4_with_the_pinned_taps_switch_off_a_repeat_is_answered_as_before(
    talking_world, monkeypatch
):
    seeded, phone, out = await served(talking_world)
    card = filler(out)
    await phone.tap(card, out)
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    talking_world.refreshes.clear()

    again = await phone.tap(card, out)

    assert again.refused is None and again.accepted.suggestion_outcome is None
    assert again.new_drafts == 0 and again.before == again.after
    assert "pinned_stale" not in h.world_refreshes(talking_world)
    assert "refused" not in (phone.ring_entry(out.set_id) or {})
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
