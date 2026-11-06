"""R5 defect 8: a restating tap on an account on approval readiness (KRI-721).

Failure-mode list (written before the code; each row is a fixture below).

A filler or mute tap re-drafts the thread's plan with one more requirement and asks for the render
approval. For an account on approval readiness the draft is judged by ``assess_readiness`` against
``planned.readiness_manifest``, which ``_plan_with_live_agent`` resolves and the pinned planner
never did, so every requirement answered "I haven't established which renderer can make this edit
yet" and EVERY filler or mute tap was refused ``requirements_not_ready``. The serve covered it by
holding the cards back from those accounts whole (``_restate_held``).

  R1  A held card, the switch flipped after the serve: the account is on readiness when it taps a
      card it was served earlier. The tap stages the draft and its approval (a refusal is a dead
      tap).
  R2  An account on readiness from the start is served the restating cards, and each one executes:
      filler on a Talking take, mute on a montage (the dark constant patched on).
  R3  The staged approval can be approved: the approval-time recheck resolves the same manifest, so
      the render is dispatched with the card's requirement in force.
  R4  The serve's other withholding still holds on a readiness account: the east-run brief asks
      for a landmark on every clip, which a single talking take cannot carry, so the filler card
      is not served, and every card that IS served executes.
  R5  Off is off: an account NOT on readiness resolves nothing (``readiness_manifest`` stays None,
      the draft is what it was).
  R6  A tap that restates nothing (an editor card) resolves nothing either way: no manifest read.

The driver is the real path: GET -> worker -> GET -> ``submit_turn`` -> ``run_kria_turn``.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import approval_readiness
from app.models import CreatorAgentExecution
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h
from tests.tasks.test_kria_pinned_r4_withdraw import card_of, prefs

_dispose_async_engine = e2e_world._dispose_async_engine
world = e2e_world.world
talking_world = e2e_world.talking_world

KEY = "nermin-matcha-update"


def plan_names_its_clips(seeded) -> None:  # noqa: ANN001
    """A plan drafted under approval readiness names the clips it renders: the renderer refuses
    one that selects none ("native rendering requires at least one attached clip"). The replay
    fixtures' hand-written plans select none, which readiness is not asked about elsewhere."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.database import sync_session
    from app.models import CreatorEditDraft, PlanItem

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        draft = db.execute(
            h.select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == seeded.thread_id,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        snapshot = dict(draft.snapshot_json)
        snapshot["strategy"] = {
            **snapshot["strategy"],
            "selected_media_ids": [f"clip-{i + 1}" for i in range(len(item.clip_gcs_paths or []))],
        }
        draft.snapshot_json = snapshot
        flag_modified(draft, "snapshot_json")
        db.commit()


async def served(world, profile=TALKING, key=KEY):  # noqa: ANN001, ANN201
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    plan_names_its_clips(seeded)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


def readiness_on(monkeypatch) -> None:  # noqa: ANN001
    """The account is on approval readiness, and the renderer's capabilities are the product's.

    ``approval_readiness`` binds ``resolve_item_creator_context`` by name when it is first
    imported; imported for the first time under the shared harness (which replaces that function
    with a manifest that has no media and no render capabilities), it would keep the stand-in for
    the rest of the process and refuse every plan.
    """
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(
        approval_readiness, "resolve_item_creator_context", d.REAL_MANIFEST_RESOLVER
    )


def kinds(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


@pytest.mark.asyncio
async def test_r1_a_held_card_is_not_refused_once_the_account_is_on_readiness(
    talking_world, monkeypatch
):
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    readiness_on(monkeypatch)

    tap = await phone.tap(card, out)

    assert tap.refused is None, (tap.refused, tap.reply)
    assert tap.planned and tap.new_drafts == 1
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
    assert d.approvals_pending(seeded.thread_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["filler_words", "mute_clip"])
async def test_r2_an_account_on_readiness_is_served_the_restating_cards_and_they_run(
    talking_world, monkeypatch, kind
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    readiness_on(monkeypatch)
    seeded, phone, out = await served(talking_world, TALKING if kind == "filler_words" else SIGNALS)

    assert kind in kinds(out), kinds(out)
    tap = await phone.tap(card_of(out, kind), out)

    assert tap.refused is None, (tap.refused, tap.reply)
    assert tap.planned and tap.new_drafts == 1
    assert d.approvals_pending(seeded.thread_id) == 1


@pytest.mark.asyncio
async def test_r3_the_staged_approval_is_approved_and_dispatches(talking_world, monkeypatch):
    readiness_on(monkeypatch)
    seeded, phone, out = await served(talking_world)
    tap = await phone.tap(card_of(out, "filler_words"), out)
    assert tap.planned, (tap.refused, tap.reply)

    dispatch = await phone.approve_render(monkeypatch)

    assert dispatch.decision == "approved", dispatch
    assert len(dispatch.calls) == 1, "the render was handed off once"
    binding = (dispatch.calls[0].get("creator_brief_binding") or {}).get("brief") or {}
    facts = [r.get("facts") or {} for r in binding.get("requirements") or []]
    assert {"speech_cleanup": True} in facts, facts


@pytest.mark.asyncio
async def test_r4_a_plan_that_cannot_carry_the_ask_still_serves_no_filler_card(
    talking_world, monkeypatch
):
    readiness_on(monkeypatch)
    scn = threads.SCENARIOS["east-run-brief"]
    seeded = threads.seed_thread(scn, TALKING)
    threads.seed_cleanup_analysis(seeded, language=scn.language)
    import json

    from tests.tasks.test_kria_pinned_r4_withdraw import CASSETTES

    cassette = json.loads((CASSETTES / "east-run-brief.json").read_text("utf-8"))["prefs"]
    phone = Phone(seeded, talking_world, cassette, language=scn.language)
    out = await phone.serve()

    assert "filler_words" not in kinds(out), kinds(out)
    for kind in kinds(out):
        twin = threads.seed_thread(scn, TALKING)
        threads.seed_cleanup_analysis(twin, language=scn.language)
        twin_phone = Phone(twin, talking_world, cassette, language=scn.language)
        twin_out = await twin_phone.serve()
        tap = await twin_phone.tap(card_of(twin_out, kind), twin_out)
        assert tap.planned and tap.refused is None, (kind, tap.refused, tap.reply)


@pytest.mark.asyncio
async def test_r5_off_resolves_no_manifest(talking_world, monkeypatch):
    calls: list[object] = []
    real = approval_readiness.resolve_readiness_manifest

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(approval_readiness, "resolve_readiness_manifest", spy)
    seeded, phone, out = await served(talking_world)
    tap = await phone.tap(card_of(out, "filler_words"), out)

    assert tap.planned, (tap.refused, tap.reply)
    assert calls == [], "an account not on readiness never reads the readiness manifest"
    assert not _stored_readiness(seeded)


def _stored_readiness(seeded) -> bool:  # noqa: ANN001
    from app.database import sync_session

    with sync_session() as db:
        rows = db.execute(h.select(CreatorAgentExecution.result)).scalars().all()
    return any(isinstance(r, dict) and r.get("approval_readiness") for r in rows)


@pytest.mark.asyncio
async def test_r6_an_editor_card_reads_no_manifest(world, monkeypatch):
    readiness_on(monkeypatch)
    calls: list[object] = []
    real = approval_readiness.resolve_readiness_manifest

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(approval_readiness, "resolve_readiness_manifest", spy)
    seeded, phone, out = await served(world, EDITOR)
    tap = await phone.tap(card_of(out, "captions"), out)

    assert tap.refused is None and tap.new_drafts == 1, tap.refused
    assert calls == [], "only a plan that asks for a render needs the renderer's capabilities"
