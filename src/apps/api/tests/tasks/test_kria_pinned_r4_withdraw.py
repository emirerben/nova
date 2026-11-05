"""A staged filler render that never happens takes its tap back (deny, expiry, a newer video).

Failure catalogue (written before the code; each row is a fixture below).

A filler tap writes three things besides the draft: it flips ``item.speech_cleanup_enabled`` and
``user_edited``, it writes a live ``speech_cleanup: True`` requirement the creator never typed, and
it marks the card applied on its ring entry. Only Undo took them back. So a creator who tapped the
card, read the approval and pressed "No" (or let it lapse) was left with cleanup switched on for
the next render, a requirement KRI-129 reads as their own ask, and a card that stays spent.

  G1  Deny the staged render: the item flags, the brief and the card are what they were before the
      tap. The card is an offer again and one re-tap stages exactly one approval (not a stack).
  G2  Lazy expiry: the approval lapses and the creator writes again (``submit_turn`` closes it).
      Same state as G1.
  G3  The expiry sweep (no creator in the loop; it locks the plan item first). Same state as G1.
  G4  A newer video: the session moved on (an editor-op tap re-synced its pointer), so approving
      the staged render is refused as stale and the approval is closed. Same state as G1.
  G5  What is NOT withdrawn: a setting the creator changed themselves after the tap (they turned
      cleanup off, then declined) keeps their choice; a requirement a later turn rewrote stays.
  G6  An approval that was APPROVED is a render in flight: nothing is withdrawn.
  G7  A mute tap (the dark kind, constant patched on) withdraws its typed requirement on deny.
  G8  An approval no card staged is untouched by all of this (byte-for-byte as before).
  G9  Undo still takes the tap back exactly as it did (the shared body moved).
  G10 The creator cancels the awaiting turn: same state as G1.
  G11 The approval was APPROVED, then the render worker found the video had moved on before it
      claimed it (``approval_target_stale``): no render ever starts, so the tap is taken back.
  G12 The worker refused the claim for a missing voiceover (``voiceover_required``): same.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval
from app.models import CreationThread, CreatorAgentApproval, CreatorAgentSession
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_kinds import _PROBLEM  # noqa: F401 - the guard runs on import

# The end-to-end proof's world (every v2 flag on, models and brokers poisoned, an in-memory Redis),
# bound by assignment so the fixtures are this module's own.
_dispose_async_engine = e2e_world._dispose_async_engine
world = e2e_world.world
talking_world = e2e_world.talking_world

CASSETTES = Path(__file__).resolve().parents[1] / "e2e_suggestions" / "cassettes"
KEY = "nermin-matcha-update"


def prefs() -> list[dict]:
    path = CASSETTES / f"{KEY}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def served(world, profile=TALKING):  # noqa: ANN001, ANN201
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


def card_of(out, kind):  # noqa: ANN001, ANN201
    return next(c for c in out.suggestions if c.kind == kind)


def pending_approval(seeded) -> CreatorAgentApproval:  # noqa: ANN001
    with sync_session() as db:
        return db.execute(
            h.select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == seeded.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()


def approval_status(seeded) -> str:  # noqa: ANN001
    with sync_session() as db:
        return db.execute(
            h.select(CreatorAgentApproval.status).where(
                CreatorAgentApproval.thread_id == seeded.thread_id
            )
        ).scalar_one()


async def decide(seeded, decision: str):  # noqa: ANN001, ANN201
    approval = pending_approval(seeded)
    with sync_session() as db:
        revision = db.get(CreationThread, seeded.thread_id).revision
    async with AsyncSessionLocal() as db:
        return await decide_approval(
            db,
            thread_id=seeded.thread_id,
            approval_id=approval.id,
            creator_id=seeded.user_id,
            decision=decision,
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=approval_fingerprint(approval),
            ),
        )


def lapse(seeded) -> None:  # noqa: ANN001
    """The approval's time ran out (what a day of nobody deciding does)."""
    from datetime import UTC, datetime, timedelta

    with sync_session() as db:
        approval = db.execute(
            h.select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == seeded.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        approval.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()


def ring_consumed(phone, set_id: str) -> dict:  # noqa: ANN001
    return (phone.ring_entry(set_id) or {}).get("consumed") or {}


class Before:
    """What the creator had before the tap: the item flags and the live requirements."""

    def __init__(self, seeded) -> None:  # noqa: ANN001
        self.flags = d.item_flags(seeded)
        self.brief = d.live_brief(seeded.thread_id)


def assert_taken_back(seeded, phone, tap, before: Before) -> None:  # noqa: ANN001
    assert d.item_flags(seeded) == before.flags, "the setting the tap flipped is back"
    assert d.live_brief(seeded.thread_id) == before.brief, "the requirement the tap wrote is gone"
    assert ring_consumed(phone, tap.set_id) == {}, "the card is an offer again"


async def staged(talking_world):  # noqa: ANN001, ANN201
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    before = Before(seeded)
    tap = await phone.tap(card, out)
    assert tap.planned and tap.result["status"] == "awaiting_approval", tap.result
    assert d.item_flags(seeded)["speech_cleanup_enabled"], "the tap flipped cleanup on"
    return seeded, phone, out, card, before, tap


@pytest.mark.asyncio
async def test_g1_a_denied_render_takes_its_tap_back_and_the_card_is_an_offer_again(talking_world):
    seeded, phone, out, card, before, tap = await staged(talking_world)

    decision, _successor = await decide(seeded, "deny")

    assert decision.status == "denied"
    assert_taken_back(seeded, phone, tap, before)
    again = await phone.tap(card, out)
    assert again.planned and again.new_drafts == 1, (again.pin, again.refused)
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
    assert approval_status_count(seeded, "pending") == 1, "one approval waits, never a stack"


def approval_status_count(seeded, status: str) -> int:  # noqa: ANN001
    with sync_session() as db:
        return len(
            db.execute(
                h.select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == seeded.thread_id,
                    CreatorAgentApproval.status == status,
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_g2_a_lapsed_approval_closed_by_the_creators_next_message_takes_its_tap_back(
    talking_world,
):
    seeded, phone, out, card, before, tap = await staged(talking_world)
    lapse(seeded)

    await h.submit(seeded, "Make the intro shorter")

    assert approval_status(seeded) == "expired"
    assert_taken_back(seeded, phone, tap, before)


@pytest.mark.asyncio
async def test_g3_the_expiry_sweep_takes_its_tap_back(talking_world):
    from app.tasks.kria_runtime import _expire_pending_approvals

    seeded, phone, out, card, before, tap = await staged(talking_world)
    lapse(seeded)

    _expire_pending_approvals()

    assert approval_status(seeded) == "expired"
    assert_taken_back(seeded, phone, tap, before)
    again = await phone.tap(card, out)
    assert again.planned and again.new_drafts == 1, (again.pin, again.refused)


@pytest.mark.asyncio
async def test_g4_approving_a_staged_render_the_session_moved_on_from_takes_its_tap_back(
    talking_world,
):
    seeded, phone, out, card, before, tap = await staged(talking_world)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = "generation-9"  # an editor-op tap read a newer video
        db.commit()

    with pytest.raises(RuntimeFailure) as stale:
        await decide(seeded, "approve")

    assert stale.value.code == "approval_target_stale"
    assert approval_status(seeded) == "cancelled"
    assert_taken_back(seeded, phone, tap, before)


@pytest.mark.asyncio
async def test_g5_what_the_creator_changed_since_the_tap_is_theirs(talking_world):
    seeded, phone, out, card, before, tap = await staged(talking_world)
    with sync_session() as db:  # the creator turned cleanup off themselves, then declined
        item = db.get(h.PlanItem, seeded.item_id, with_for_update=True)
        item.speech_cleanup_enabled = False
        item.user_edited = True
        db.commit()

    await decide(seeded, "deny")

    flags = d.item_flags(seeded)
    assert flags == {"speech_cleanup_enabled": False, "user_edited": True}, flags
    assert d.live_brief(seeded.thread_id) == before.brief


@pytest.mark.asyncio
async def test_g6_an_approved_render_is_not_taken_back(talking_world, monkeypatch):
    seeded, phone, out, card, before, tap = await staged(talking_world)

    dispatch = await phone.approve_render(monkeypatch)

    assert dispatch.decision == "approved"
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
    assert any(r["facts"].get("speech_cleanup") is True for r in d.live_brief(seeded.thread_id))
    assert ring_consumed(phone, tap.set_id) == {card.id: tap.accepted.turn_id}


@pytest.mark.asyncio
async def test_g7_a_denied_mute_render_withdraws_its_requirement(talking_world, monkeypatch):
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    seeded, phone, out = await served(talking_world, SIGNALS)
    card = card_of(out, "mute_clip")
    before = Before(seeded)
    tap = await phone.tap(card, out)
    assert tap.planned and len(d.live_brief(seeded.thread_id)) == len(before.brief) + 1

    await decide(seeded, "deny")

    assert_taken_back(seeded, phone, tap, before)


@pytest.mark.asyncio
async def test_g8_an_approval_no_card_staged_is_denied_as_before(talking_world, monkeypatch):
    """A typed request's approval carries no footprint: deny changes nothing but its status."""
    seeded, phone, out = await served(talking_world)
    flags_before = d.item_flags(seeded)
    brief_before = d.live_brief(seeded.thread_id)
    card = card_of(out, "filler_words")
    tap = await phone.tap(card, out)
    # Strip the footprint, as a draft minted by a typed turn would not have one.
    with sync_session() as db:
        from app.models import CreatorAgentExecution

        for execution in db.execute(h.select(CreatorAgentExecution)).scalars():
            if (execution.result or {}).get("pinned_undo"):
                execution.result = {k: v for k, v in execution.result.items() if k != "pinned_undo"}
        db.commit()
    flags_tapped = d.item_flags(seeded)
    brief_tapped = d.live_brief(seeded.thread_id)

    await decide(seeded, "deny")

    assert d.item_flags(seeded) == flags_tapped != flags_before
    assert d.live_brief(seeded.thread_id) == brief_tapped != brief_before
    assert ring_consumed(phone, tap.set_id) == {card.id: tap.accepted.turn_id}


@pytest.mark.asyncio
async def test_g9_undo_still_takes_the_tap_back(talking_world):
    seeded, phone, out, card, before, tap = await staged(talking_world)

    await phone.undo()

    assert_taken_back(seeded, phone, tap, before)


@pytest.mark.asyncio
async def test_g10_cancelling_the_awaiting_turn_takes_its_tap_back(talking_world):
    from app.kria.runtime import cancel_turn

    seeded, phone, out, card, before, tap = await staged(talking_world)
    with sync_session() as db:
        revision = db.get(CreationThread, seeded.thread_id).revision
    async with AsyncSessionLocal() as db:
        await cancel_turn(
            db,
            thread_id=seeded.thread_id,
            turn_id=uuid.UUID(tap.accepted.turn_id),
            creator_id=seeded.user_id,
            expected_thread_revision=revision,
        )

    assert approval_status(seeded) == "cancelled"
    assert_taken_back(seeded, phone, tap, before)


def newer_video(seeded):  # noqa: ANN001, ANN201
    """What an editor-op tap (or a Kria render) writes: the session's pointer moves on."""

    def move() -> None:
        with sync_session() as db:
            session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
            session.target_generation_id = "generation-9"
            db.commit()

    return move


@pytest.mark.asyncio
async def test_g11_a_claim_that_finds_the_video_moved_takes_its_tap_back(
    talking_world, monkeypatch
):
    seeded, phone, out, card, before, tap = await staged(talking_world)

    dispatch = await phone.approve_render(monkeypatch, between=newer_video(seeded))

    assert approval_status(seeded) == "cancelled"
    assert dispatch.calls == [], "no render started"
    assert_taken_back(seeded, phone, tap, before)


@pytest.mark.asyncio
async def test_g12_a_claim_refused_for_a_missing_voiceover_takes_its_tap_back(
    talking_world, monkeypatch
):
    seeded, phone, out, card, before, tap = await staged(talking_world)

    def needs_a_voiceover() -> None:
        from sqlalchemy.orm.attributes import flag_modified

        from app.models import CreatorEditDraft

        with sync_session() as db:
            draft = db.execute(
                h.select(CreatorEditDraft).where(
                    CreatorEditDraft.thread_id == seeded.thread_id,
                    CreatorEditDraft.is_head.is_(True),
                )
            ).scalar_one()
            snapshot = dict(draft.snapshot_json)
            snapshot["strategy"] = {**snapshot["strategy"], "audio_strategy": "voiceover"}
            draft.snapshot_json = snapshot
            flag_modified(draft, "snapshot_json")
            item = db.get(h.PlanItem, seeded.item_id, with_for_update=True)
            item.voiceover_gcs_path = None
            db.commit()

    dispatch = await phone.approve_render(monkeypatch, between=needs_a_voiceover)

    assert dispatch.calls == [], "no render started"
    assert approval_status(seeded) == "cancelled"
    assert_taken_back(seeded, phone, tap, before)

