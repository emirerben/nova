"""A restating card and a native-editor Save: a restated plan holds none of the Save's edits, so
a restating card is offered, and runs, only while the agent session has read the saved video.

Failure catalogue (written before the code; each row is a fixture below).

``_restate_video_moved`` compares the session's pointer (the generation the agent last read) with
the saved variant. A native Save writes the variant and never the pointer (about thirty writers
move a variant's generation; only a Kria render or an editor-op tap re-syncs it). A restating tap
re-drafts the thread's PLAN and its approval mints a new Job from that plan, so whenever a Save
the session never read is on the variant, running the card would drop the Save's edits. That holds
whatever the card was served after; the card's serve time says nothing about the plan.

  F1  A card served BEFORE a Save, tapped after it: refused ``base_moved``; no draft, approval,
      brief version or setting, and the session pointer is untouched by the refusal.
  F2  A card served AFTER a Save is never offered (the serve asks the tap's own question), so no
      creator meets a dead first tap, and no tap can run on a plan that lacks their Save. The cards
      that do not restate (the editor kinds) are still offered.
  F3  The Save lands after planning but before the row lock (the plan read generation N, the lock
      finds N+1): refused, by the planner-side witness.
  F4  The agent reads the saved video (a Kria render completes and re-syncs the pointer): the card
      is offered again and its tap runs. The withholding lasts only while the Save is unread.
  F5  A session that has not read any video yet (no pointer): nothing to compare; offered and runs.
  F6  An editor-op card is untouched by all of this (it is built on the saved video).

The Save is the real ``POST /plan-items/{id}/variants/{id}/editor-commit`` (through the ASGI app,
only the creator's identity overridden); a Save that must land between the plan and the lock (F3)
calls ``prepare_editor_commit``, the function the route calls, because it runs inside the worker.
The render worker's completion is the one stand-in (``finish_render``): no worker runs here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.database import sync_session
from app.models import CreatorAgentSession, Job
from tests.e2e_suggestions import conftest as e2e_world
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.threads import EDITOR, TALKING
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


def _stamp() -> str:
    # The wire format every render stamp uses: naive UTC with a literal "Z".
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


async def served(world, profile=TALKING):  # noqa: ANN001, ANN201
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return seeded, phone, await phone.serve()


def card_of(out, kind):  # noqa: ANN001, ANN201
    return next(c for c in out.suggestions if c.kind == kind)


def native_save(seeded, *, finish: bool = True, **sections) -> str:  # noqa: ANN001, ANN003
    """The creator's Save through the editor's own commit function, then (``finish``) the render
    worker's completion write. Returns the generation the Save minted. ``sections`` replaces the
    default (a caption size, which a Talking take accepts)."""
    from app.routes.generative_jobs import (
        EditorCommitCaptionMeta,
        EditorCommitRequest,
        prepare_editor_commit,
        variant_render_baseline,
    )

    with sync_session() as db:
        job = db.get(Job, seeded.job_id, with_for_update=True)
        variant = job.assembly_plan["variants"][0]
        prep = prepare_editor_commit(
            job,
            variant["variant_id"],
            EditorCommitRequest(
                **(sections or {"caption_meta": EditorCommitCaptionMeta(size_px=96)}),
                base_generation=variant_render_baseline(variant),
            ),
            user_id=str(seeded.user_id),
            plan_item_id=str(seeded.item_id),
        )
        db.commit()
    if finish:
        finish_render(seeded)
    return str(prep["generation"])


async def route_save(seeded, **sections) -> str:  # noqa: ANN001, ANN003
    """The creator's Save through the real editor-commit route, then the render worker's
    completion write. Returns the generation the Save minted."""
    from types import SimpleNamespace
    from unittest import mock

    import httpx

    from app.auth import get_current_user
    from app.main import app
    from app.routes.generative_jobs import variant_render_baseline

    with sync_session() as db:
        variant = db.get(Job, seeded.job_id).assembly_plan["variants"][0]
    body = {**(sections or {"caption_meta": {"size_px": 96}})}
    body["base_generation"] = variant_render_baseline(variant)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=seeded.user_id)
    try:
        # The render kick is the one thing the route hands to a broker.
        with mock.patch("app.routes.plan_items.enqueue_editor_commit_render"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://phone"
            ) as client:
                response = await client.post(
                    f"/plan-items/{seeded.item_id}/variants/{variant['variant_id']}/editor-commit",
                    json=body,
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, response.text[:300]
    finish_render(seeded)
    return response.json()["generation"]


def finish_render(seeded) -> None:  # noqa: ANN001
    """What the render worker writes when the Save's render lands."""
    h.edit_variant(
        seeded, lambda v: v.update(render_status="ready", render_finished_at=_stamp(), ok=True)
    )


def pointer(seeded) -> str | None:  # noqa: ANN001
    with sync_session() as db:
        return db.get(CreatorAgentSession, seeded.session_id).target_generation_id


def untouched(tap: d.Tap) -> bool:
    return tap.new_drafts == 0 and tap.before == tap.after


def agent_reads_video(seeded) -> str:  # noqa: ANN001
    """What a Kria render's completion writes: the session's pointer follows the saved video."""
    with sync_session() as db:
        generation = variant_baseline(db.get(Job, seeded.job_id).assembly_plan["variants"][0])
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = generation
        db.commit()
    return generation


def variant_baseline(variant: dict) -> str:
    from app.routes.generative_jobs import variant_render_baseline

    return variant_render_baseline(variant)


@pytest.mark.asyncio
async def test_f1_a_card_served_before_a_save_is_refused_after_it(talking_world):
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    before_pointer = pointer(seeded)
    generation = await route_save(seeded)
    assert generation != before_pointer

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)
    assert not d.item_flags(seeded)["speech_cleanup_enabled"]
    assert pointer(seeded) == before_pointer, "a refused tap leaves the session's pointer alone"


@pytest.mark.asyncio
async def test_f2_a_card_served_after_a_save_is_never_offered(talking_world):
    seeded, phone, first = await served(talking_world)
    assert "filler_words" in {c.kind for c in first.suggestions}
    generation = await route_save(seeded)
    talking_world.refreshes.clear()

    out = await phone.serve()

    assert pointer(seeded) != generation, "a Save never moves the agent's pointer"
    kinds = {c.kind for c in out.suggestions}
    assert out.set_id != first.set_id
    assert "filler_words" not in kinds, kinds
    assert kinds, "the cards that do not restate the plan are still offered"


@pytest.mark.asyncio
async def test_f3_a_save_after_planning_is_still_refused(talking_world, monkeypatch):
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    h.after_planning(monkeypatch, lambda: native_save(seeded))

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)
    assert not d.item_flags(seeded)["speech_cleanup_enabled"]


@pytest.mark.asyncio
async def test_f4_the_card_returns_once_the_agent_has_read_the_saved_video(talking_world):
    seeded, phone, _first = await served(talking_world)
    await route_save(seeded)
    generation = agent_reads_video(seeded)
    talking_world.refreshes.clear()

    out = await phone.serve()
    card = card_of(out, "filler_words")
    tap = await phone.tap(card, out)

    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
    assert d.head_of(seeded.thread_id)["gen"] == generation


@pytest.mark.asyncio
async def test_f5_a_session_with_no_pointer_has_nothing_to_compare(talking_world):
    seeded, phone, first = await served(talking_world)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = None
        db.commit()
    await route_save(seeded)
    talking_world.refreshes.clear()

    out = await phone.serve()
    card = card_of(out, "filler_words")
    tap = await phone.tap(card, out)

    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1


@pytest.mark.asyncio
async def test_f6_an_editor_op_card_is_built_on_the_saved_video_and_still_applies(world):
    seeded, phone, out = await served(world, EDITOR)
    card = card_of(out, "transition")
    generation = await route_save(seeded, text_elements=[{**h.HOOK, "text": "Saved by hand"}])

    tap = await phone.tap(card, out)

    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert d.head_of(seeded.thread_id)["gen"] == generation
