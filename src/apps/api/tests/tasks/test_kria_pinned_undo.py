"""Undo takes back everything a card tap did, so the card is not stranded and the brief is clean.

Failure catalogue, written before any seam code. A pinned tap writes more than a draft revision: a
typed brief requirement (the receipt names it, a later replan reads it) and, for the filler card,
an item setting flipped inside the same transaction. ``undo_draft`` restored the parent draft and
withdrew the approval and nothing else. Every row drives submit -> claim -> plan -> row-locked
commit through ``run_kria_turn`` on real Postgres, then the real ``undo_draft``.

  U1  Filler: Undo leaves ``speech_cleanup_enabled`` and ``user_edited`` True and the
      "Cut the filler words" requirement live. The card is then consumed, so tapping it again is
      "no longer fits", and the next approved render cuts speech the creator undid.
  U2  Hook: Undo leaves the approved-copy title requirement live. The next restating tap is held
      back by approval readiness ("Not everything you asked for made it in: Add the hook shown on
      the card"), and KRI-129 hides the hook card for a title the creator no longer has.
  U3  Mute: Undo leaves the ``clip_sound off`` requirement live, which KRI-129 reads as a sound
      ask already on the clip, so the very card that was undone is refused.
  U4  Editor-op cards (sound effects, duplicates): the same leftover requirement ("Remove clip 1"
      names a clip, so the duplicates card refuses itself after Undo).
  U5  The setting goes back to what it was BEFORE the tap (``user_edited`` too), not to a default:
      a creator who had already edited the item keeps that fact.
  U6  Nothing the creator did since is undone with it: a requirement they reworded (it is theirs
      now), a setting they flipped back themselves, and the earlier tap's requirement when the
      Undo is of a later draft.
  U7  Undo of Undo (the restored revision is itself undoable) must not crash or write another
      brief version. (A plain draft with no tap behind it undoes exactly as before: the existing
      undo fixtures in ``test_kria_pinned_kinds`` / ``test_kria_pinned_suggestion`` pin that.)
  U8  Concurrency: the withdrawal binds the brief version it reads under the thread lock, so a
      version written between the tap and the Undo is neither lost nor clobbered.

"Not stranded" is proved at the card: tap, Undo, tap the SAME card again, and the live
requirements are what they were after the first tap. The brief is append-only, so "identical"
means the same live requirements (the withdrawn one stays on record as superseded by the server).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import settings
from app.database import engine as async_engine
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_kinds import (  # noqa: F401 - the guard runs on import
    _PROBLEM,
    HOOK_TEXT,
    approvals_pending,
    head,
    mute_world,
    serve_duplicates,
    serve_filler,
    serve_mute,
    signal_of,
    tap_and_run,
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    world = h.install_world(monkeypatch)
    k.enable_kinds(monkeypatch)
    return world


@pytest.fixture
def published(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit",
        lambda analysis_id: calls.append(analysis_id) or True,
    )
    return calls


def live(thread_id) -> list[dict]:  # noqa: ANN001
    """The live requirements of the latest brief version, without server ids or turn ids."""
    versions = h.briefs(thread_id)
    if not versions:
        return []
    return [
        {key: value for key, value in req.items() if key not in {"id", "source_turn_id"}}
        for req in versions[-1].requirements
        if req.get("status") != "superseded"
    ]


async def undo(seeded) -> None:  # noqa: ANN001
    from app.kria.drafts import undo_draft

    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )


# ── U1/U5: the filler card ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("user_edited_before", [False, True])
@pytest.mark.asyncio
async def test_filler_undo_withdraws_the_setting_and_the_requirement_and_the_card_works_again(
    world, monkeypatch, published, user_edited_before
):
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    k.set_item(seeded, user_edited=user_edited_before)
    item_before, live_before = k.item_row(seeded), live(seeded.thread_id)
    assert item_before["speech_cleanup_enabled"] is False

    _, first = await tap_and_run(seeded, "filler_words")
    assert first["status"] == "awaiting_approval", first
    assert k.item_row(seeded)["speech_cleanup_enabled"] is True
    live_after_tap = live(seeded.thread_id)
    assert live_after_tap != live_before

    await undo(seeded)

    assert approvals_pending(seeded.thread_id) == 0
    assert k.item_row(seeded) == item_before, "the setting is what it was before the tap"
    assert live(seeded.thread_id) == live_before, "the tap's requirement is withdrawn"
    withdrawn = h.briefs(seeded.thread_id)[-1].requirements[-1]
    assert withdrawn["status"] == "superseded"
    assert withdrawn["facts"]["superseded_by"] == "server", "a waiver the server made"

    _, again = await tap_and_run(seeded, "filler_words")  # the very same card, the very same set
    assert again["status"] == "awaiting_approval", again
    assert signal_of(seeded.thread_id) is None
    assert k.item_row(seeded)["speech_cleanup_enabled"] is True
    assert live(seeded.thread_id) == live_after_tap


# ── U2: the hook card ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_undo_withdraws_the_approved_copy_requirement(world, monkeypatch, published):
    """After Undo the title is gone and so is the requirement that said the title was wanted.

    The harness has no renderer routing, so approval readiness refuses a restating tap for its own
    sake ("I haven't established which renderer can make this edit yet"). What this proves is what
    that refusal lists: the tap's own ask, not a hook the creator took back."""
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    k.filler_world(monkeypatch)
    k.seed_talking_item(seeded)
    k.seed_strategy_draft(seeded)
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("filler_words", rank=2))
    live_before = live(seeded.thread_id)

    _, tapped = await tap_and_run(seeded, "hook")
    assert tapped["status"] == "completed", tapped
    [title] = [r for r in live(seeded.thread_id) if r["scope"] == "title"]
    assert title["literal"] == HOOK_TEXT

    await undo(seeded)

    assert live(seeded.thread_id) == live_before
    assert head(seeded.thread_id)["snap"]["kind"] == "strategy"
    await tap_and_run(seeded, "filler_words")
    reply = [e for e in h.events(seeded.thread_id) if e.role == "assistant"][-1].content
    assert "Cut the filler words" in reply
    assert "hook" not in reply.casefold(), "the refusal lists a hook the creator took back"


# ── U3: the mute card ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mute_undo_withdraws_the_sound_requirement_and_the_card_works_again(
    world, monkeypatch
):
    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    live_before = live(seeded.thread_id)

    _, first = await tap_and_run(seeded, "mute_clip")
    assert first["status"] == "awaiting_approval", first
    live_after_tap = live(seeded.thread_id)
    assert live_after_tap[-1]["facts"] == {"clip_sound": "off"}

    await undo(seeded)

    assert live(seeded.thread_id) == live_before
    _, again = await tap_and_run(seeded, "mute_clip")
    assert again["status"] == "awaiting_approval", again
    assert signal_of(seeded.thread_id) is None
    assert live(seeded.thread_id) == live_after_tap


# ── U4: editor-op cards ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicates_undo_withdraws_the_removal_requirement_and_the_card_works_again(
    world, monkeypatch
):
    seeded = h.seed_ready_thread()
    await serve_duplicates(seeded, monkeypatch, also_transition=True)
    await tap_and_run(seeded, "sfx")  # a head draft to follow, and Undo needs a parent
    live_before = live(seeded.thread_id)

    _, first = await tap_and_run(seeded, "duplicates")
    assert first["status"] == "completed", first
    live_after_tap = live(seeded.thread_id)
    assert live_after_tap[-1]["scope"] == "clip:clip-a"

    await undo(seeded)

    assert live(seeded.thread_id) == live_before
    _, again = await tap_and_run(seeded, "duplicates")
    assert again["status"] == "completed", again
    assert signal_of(seeded.thread_id) is None
    assert live(seeded.thread_id) == live_after_tap


@pytest.mark.asyncio
async def test_sound_effect_undo_withdraws_its_requirement(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await tap_and_run(seeded, "transition")  # a head draft to follow
    live_before = live(seeded.thread_id)

    await tap_and_run(seeded, "sfx")
    assert live(seeded.thread_id) != live_before
    await undo(seeded)

    assert live(seeded.thread_id) == live_before
    _, again = await tap_and_run(seeded, "sfx")
    assert again["status"] == "completed" and signal_of(seeded.thread_id) is None


# ── U6: nothing the creator did since is undone with it ──────────────────────────────


@pytest.mark.asyncio
async def test_undo_of_a_later_draft_leaves_the_earlier_taps_requirement(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await tap_and_run(seeded, "sfx")
    with_sfx = live(seeded.thread_id)
    await tap_and_run(seeded, "transition")
    assert len(live(seeded.thread_id)) == len(with_sfx) + 1

    await undo(seeded)  # takes back the transition only

    assert live(seeded.thread_id) == with_sfx


@pytest.mark.asyncio
async def test_a_requirement_the_creator_reworded_since_the_tap_stays(world):
    from app.kria.brief import BriefUpdate

    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await tap_and_run(seeded, "transition")  # a head draft to follow
    await tap_and_run(seeded, "sfx")
    version = h.briefs(seeded.thread_id)[-1]
    [mine] = [r for r in version.requirements if r["description"] == "Add sound effects"]
    k.write_brief(
        seeded,
        BriefUpdate(
            operation="change",
            target_requirement_id=mine["id"],
            expected_version=version.version,
            description="Add two sound effects, quietly",
        ),
    )
    reworded = live(seeded.thread_id)

    await undo(seeded)

    assert live(seeded.thread_id) == reworded, "the creator's wording is theirs now"


@pytest.mark.asyncio
async def test_a_setting_the_creator_flipped_back_since_the_tap_is_not_restored(
    world, monkeypatch, published
):
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    await tap_and_run(seeded, "filler_words")
    k.set_item(seeded, speech_cleanup_enabled=False, user_edited=True)  # their own PATCH

    await undo(seeded)

    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert k.item_row(seeded)["user_edited"] is True, "that edit was theirs, not the tap's"


# ── U7/U8: Undo of Undo, a plain draft, and a brief that moved ───────────────────────


@pytest.mark.asyncio
async def test_undo_of_the_undo_neither_crashes_nor_writes_another_brief_version(
    world, monkeypatch, published
):
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    await tap_and_run(seeded, "filler_words")
    await undo(seeded)
    versions = len(h.briefs(seeded.thread_id))
    live_after = live(seeded.thread_id)

    await undo(seeded)  # the restored revision is itself undoable

    assert len(h.briefs(seeded.thread_id)) == versions
    assert live(seeded.thread_id) == live_after
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False


@pytest.mark.asyncio
async def test_a_brief_version_written_between_the_tap_and_the_undo_is_kept(world):
    from app.kria.brief import BriefUpdate

    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await tap_and_run(seeded, "transition")  # a head draft to follow
    await tap_and_run(seeded, "sfx")
    k.write_brief(
        seeded, BriefUpdate(kind="text", scope="title", literal="Later", description="Add a title")
    )
    kept = [r for r in live(seeded.thread_id) if r["scope"] == "title"]

    await undo(seeded)

    after = live(seeded.thread_id)
    assert [r for r in after if r["scope"] == "title"] == kept
    assert not [r for r in after if r["description"] == "Add sound effects"]
