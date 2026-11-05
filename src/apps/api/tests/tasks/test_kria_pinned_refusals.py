"""A refused tap leaves nothing behind, and a restate only runs on the video it was planned for.

Failure catalogue, written before any seam code. Each row below is a fixture that drives submit ->
claim -> plan -> row-locked commit through ``run_kria_turn`` on real Postgres (the harness in
``kria_pinned_suggestion_harness`` / ``kria_pinned_kinds_harness``). The rule they defend: a tap
that is refused changes nothing a later turn can read, and a tap that restates the plan never
carries the creator's saved edits away.

Refused taps write nothing (GAP-6)
  G1  The approval-readiness gate refuses a restating tap and still commits the tap's typed
      requirement (``brief_updates=planned.brief_updates``): a live "Cut the filler words
      {speech_cleanup}" requirement the creator never typed, while the setting stayed off. KRI-129
      then reads it as the creator's own ask. Expected: no brief version at all.
  G2  The mark gate and the choice gate do the same (they pass the effective updates).
  G3  The reply of a refused tap points at no brief version that was never written: the receipts
      and the "make it without these" question are stamped with the version of the rolled-back
      speculative brief. A later turn that writes that number would make them read as current.
  G4  Control: the consent gate wrote nothing already, and keeps writing nothing.

A restate on a video that moved (Save without a draft)
  S1  A native-editor Save lands between the plan and the row lock and the session has seen the
      new generation: the restate must compare the generation it was planned against and refuse
      ``base_moved``. Nothing is written, the setting does not flip.
  S2  A Save landed BEFORE the tap and the session never saw it (its pointer is the old
      generation): the restated plan does not hold the saved edits, and approving it renders from
      the plan. Refused ``base_moved``.
  S3  Control: the same Save, once the session has seen it, restates normally and the draft and
      approval carry the new generation (no false refusal).
  S4  Control: an editor-op tap (captions, sound, transition) after a Save still applies; it is
      built on the saved video, so the stale pointer is re-synced as before.

Typed refusal for a restate whose clips changed
  M1  ``_complete_draft_turn`` raised ``RuntimeError("Your clips changed after planning...")``,
      which fails the turn instead of answering it. A pinned restate must roll back and answer
      ``base_moved`` like every other refusal, mark the card, and write nothing.

Database: the same CI-aware guard as the other pinned suites (importing ``test_kria_pinned_kinds``
runs it).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_kinds import (  # noqa: F401 - the guard runs on import
    _PROBLEM,
    HOOK_TEXT,
    NO_LONGER_FITS,
    RESTATE_KINDS,
    add_creator_title,
    head,
    last_reply,
    serve_restate_beside_transition,
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
    """Analyses handed to the publisher (the broker is not reached)."""
    calls: list = []
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit",
        lambda analysis_id: calls.append(analysis_id) or True,
    )
    return calls


def live(thread_id) -> list[dict]:  # noqa: ANN001
    """The live requirements of the thread's latest brief version, without their server ids."""
    versions = h.briefs(thread_id)
    if not versions:
        return []
    return [
        {key: value for key, value in req.items() if key not in {"id", "source_turn_id"}}
        for req in versions[-1].requirements
        if req.get("status") != "superseded"
    ]


def assistant_payload(thread_id) -> dict:  # noqa: ANN001
    return [e for e in h.events(thread_id) if e.role == "assistant"][-1].payload


def brief_numbers_on_the_reply(payload: dict) -> set[int]:
    """Every brief version number the reply's receipts and question name."""
    found = {r["brief_version"] for r in payload.get("requirement_receipts") or []}
    question = payload.get("choice_question")
    if isinstance(question, dict) and isinstance(question.get("brief_version"), int):
        found.add(question["brief_version"])
    return found


# ── G1/G2/G3/G4: a refused tap writes nothing to the brief ───────────────────────────


def unmeetable_title():  # noqa: ANN201
    from app.kria.brief import BriefUpdate

    return BriefUpdate(
        kind="text", scope="title", literal="Hello Moon", description="Title says Hello Moon"
    )


@pytest.mark.parametrize("gate", ["readiness", "mark", "choice"])
@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_refused_restating_tap_writes_nothing_to_the_brief(
    world, monkeypatch, published, gate, kind
):
    """G1/G2/G3: whichever gate ends the tap, the brief is exactly what it was, and the reply
    names no brief version that does not exist."""
    from app.kria.contracts import KriaTurnPlan
    from app.tasks import kria_runtime

    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    k.write_brief(seeded, unmeetable_title())
    if gate == "readiness":
        monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    else:
        asked = KriaTurnPlan(mode="respond", turn_value="question", response="Which one?")
        name, verdict = (
            ("_protected_region_gate_sync", (asked, {"requirement_ids": ["r1"]}))
            if gate == "mark"
            else ("_unresolved_choice_plan", (asked, "unresolved_choice"))
        )
        monkeypatch.setattr(kria_runtime, name, lambda *args, **kwargs: verdict)
    before = {"briefs": len(h.briefs(seeded.thread_id)), "live": live(seeded.thread_id)}

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed", result
    assert signal_of(seeded.thread_id)["outcome"] == "refused"
    assert len(h.briefs(seeded.thread_id)) == before["briefs"], "no brief version for a refusal"
    assert live(seeded.thread_id) == before["live"]
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    # Nothing on the reply points at a version that was never written.
    latest = h.briefs(seeded.thread_id)[-1].version
    assert all(
        number <= latest
        for number in brief_numbers_on_the_reply(assistant_payload(seeded.thread_id))
    ), "the reply names a brief version that was rolled back"
    # The real GET: the refused card is not offered again, and a new set releases the phone.
    again = await k.get_suggestions(seeded)
    assert again.set_id != seeded.set_id
    assert seeded.cards[kind].id not in {c.id for c in again.suggestions}


@pytest.mark.asyncio
async def test_the_consent_gate_wrote_nothing_to_the_brief_and_still_does_not(world, monkeypatch):
    """G4 (control): the creative-copy consent gate never passed updates."""
    k.install_inputs(monkeypatch, stale_copy_states=True)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("sfx", rank=2))
    add_creator_title(seeded, await h.media_digest_of(seeded))
    before = len(h.briefs(seeded.thread_id))

    await tap_and_run(seeded, "hook")

    assert signal_of(seeded.thread_id)["reason"] == "consent_required"
    assert len(h.briefs(seeded.thread_id)) == before


# ── S1-S4: a restate only runs on the video it was planned for ───────────────────────


def save_natively(seeded, generation: str, *, session_has_seen_it: bool) -> None:  # noqa: ANN001
    """What the native editor's Save does to the saved render: a new generation and the
    creator's own edits, with no draft written. The agent session's pointer follows only when
    the observer has since read the new render."""

    def saved(variant):
        variant["render_generation_id"] = generation
        variant["captions_enabled"] = True
        variant["text_elements"] = [{**variant["text_elements"][0], "text": "MY HAND EDIT"}]

    h.edit_variant(seeded, saved)
    if session_has_seen_it:
        with sync_session() as db:
            session = db.get(h.CreatorAgentSession, seeded.session_id, with_for_update=True)
            session.target_generation_id = generation
            db.commit()


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_save_between_the_plan_and_the_lock_refuses_the_restate(
    world, monkeypatch, published, kind
):
    """S1: planned against generation-1, the lock finds generation-2."""
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    h.after_planning(
        monkeypatch, lambda: save_natively(seeded, "generation-2", session_has_seen_it=True)
    )
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed", result
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"
    assert h.counts(seeded) == before, "no draft, approval, brief version or job"
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert published == []


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_restate_after_a_save_the_session_never_saw_refuses(
    world, monkeypatch, published, kind
):
    """S2: the saved edits are not in the plan the restate would re-draft and render."""
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    save_natively(seeded, "generation-2", session_has_seen_it=False)
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed", result
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"
    assert h.counts(seeded) == before
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert h.job_variant(seeded)["text_elements"][0]["text"] == "MY HAND EDIT"


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_restate_after_a_save_the_session_has_seen_still_applies(
    world, monkeypatch, published, kind
):
    """S3 (control): no false refusal, and the revision carries the generation it stands on."""
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    save_natively(seeded, "generation-2", session_has_seen_it=True)

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "awaiting_approval", result
    assert head(seeded.thread_id)["gen"] == "generation-2"
    assert signal_of(seeded.thread_id) is None


@pytest.mark.asyncio
async def test_an_editor_tap_after_a_save_the_session_never_saw_still_applies(world):
    """S4 (control): an editor-op tap is built on the saved video; the stale pointer re-syncs."""
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    save_natively(seeded, "generation-2", session_has_seen_it=False)

    _, result = await tap_and_run(seeded, "transition")

    assert result["status"] == "completed", result
    assert head(seeded.thread_id)["gen"] == "generation-2"
    assert signal_of(seeded.thread_id) is None


# ── M1: a restate whose clips changed is answered, not failed ────────────────────────


def swap_a_clip(seeded) -> None:  # noqa: ANN001
    """The creator replaced a clip's file: the item's media identity is no longer the plan's."""
    with sync_session() as db:
        item = db.get(h.PlanItem, seeded.item_id, with_for_update=True)
        rows = h.copy.deepcopy(item.clip_assignments)
        rows[0]["gcs_path"] = "users/test/replacement.mp4"
        item.clip_assignments = rows
        db.commit()


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_restate_whose_clips_changed_after_planning_is_a_typed_refusal(
    world, monkeypatch, published, kind
):
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    h.after_planning(monkeypatch, lambda: swap_a_clip(seeded))
    before = h.counts(seeded)
    card = seeded.cards[kind]

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed", result
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert signal_of(seeded.thread_id) == {
        "set_id": seeded.set_id,
        "id": card.id,
        "outcome": "refused",
        "reason": "base_moved",
    }
    assert h.turn_row(accepted.turn_id).status == "completed"
    assert h.counts(seeded) == before
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert k.ring_entry(seeded.thread_id, seeded.set_id)["refused"] == {card.id: accepted.turn_id}
    again = await k.get_suggestions(seeded)
    assert again.set_id != seeded.set_id and card.id not in {c.id for c in again.suggestions}
    assert published == []
