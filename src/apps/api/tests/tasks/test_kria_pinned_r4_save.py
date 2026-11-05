"""A restating card and a native-editor Save: the card must be judged against the video it was
served on, not against a pointer no Save moves.

Failure catalogue (written before the code; each row is a fixture below).

``_restate_video_moved`` compares the session's pointer (the generation the agent last read) with
the saved variant. A native Save writes the variant and never the pointer (about thirty writers
move a variant's generation; only a Kria render or an editor-op tap re-syncs it), so after ANY Save
the pointer is stale for good.

  F1  A card served BEFORE a Save, tapped after it: the plan the tap would restate holds none of
      the Save's edits. Refused ``base_moved``; no draft, approval, brief version or setting.
  F2  A card served AFTER a Save: the serve read the saved video, so the stale pointer says nothing
      about it. The tap runs: one draft, one approval, the setting flips, and the revision stands
      on the real generation (the pointer is aligned in the same commit, as an editor tap does).
      Before the fix the card was dead on its first tap, marked refused and hidden.
  F3  The Save lands after a served-after card is tapped but before the row lock (the plan read
      generation N, the lock finds N+1): still refused (the planner-side witness is unchanged).
  F4  A second Save after the card was served: served before THAT Save, refused, even though an
      earlier Save preceded the serve (the verdict is per card, not per thread).
  F5  A card served while the Save's render was still in flight (before it finished): the video it
      saw is not the finished one, so it is refused.
  F6  Unreadable instants (no ring time, no render stamp): fail closed, as before.
  F7  An editor-op card is untouched by all of this (it is built on the saved video).

The Save is the real ``prepare_editor_commit`` (the function the HTTP route calls); the render
worker's completion is the one stand-in (``finish_render``), because no worker runs here.
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


@pytest.mark.asyncio
async def test_f1_a_card_served_before_a_save_is_refused_after_it(talking_world):
    seeded, phone, out = await served(talking_world)
    card = card_of(out, "filler_words")
    before_pointer = pointer(seeded)
    generation = native_save(seeded)
    assert generation != before_pointer

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)
    assert not d.item_flags(seeded)["speech_cleanup_enabled"]


@pytest.mark.asyncio
async def test_f2_a_card_served_after_a_save_runs(talking_world):
    seeded, phone, _first = await served(talking_world)
    generation = native_save(seeded)
    talking_world.refreshes.clear()
    out = await phone.serve()
    card = card_of(out, "filler_words")
    assert pointer(seeded) != generation, "a Save never moves the agent's pointer"

    tap = await phone.tap(card, out)

    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert d.item_flags(seeded)["speech_cleanup_enabled"]
    assert d.head_of(seeded.thread_id)["gen"] == generation
    assert pointer(seeded) == generation


@pytest.mark.asyncio
async def test_f3_a_save_after_planning_is_still_refused_for_a_card_served_after_a_save(
    talking_world, monkeypatch
):
    seeded, phone, _first = await served(talking_world)
    native_save(seeded)
    out = await phone.serve()
    card = card_of(out, "filler_words")
    h.after_planning(monkeypatch, lambda: native_save(seeded))

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)
    assert not d.item_flags(seeded)["speech_cleanup_enabled"]


@pytest.mark.asyncio
async def test_f4_the_verdict_is_per_card_a_second_save_refuses_the_card_served_between(
    talking_world,
):
    seeded, phone, _first = await served(talking_world)
    native_save(seeded)
    out = await phone.serve()
    card = card_of(out, "filler_words")
    native_save(seeded)

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)


def set_entry_time(seeded, set_id: str, created_at: str | None) -> None:  # noqa: ANN001
    """Rewrite when the ring says the set was served (what the GET wrote as ``created_at``)."""
    with sync_session() as db:
        from app.models import CreationThread

        thread = db.get(CreationThread, seeded.thread_id, with_for_update=True)
        state = dict(thread.state)
        ring = []
        for entry in state["suggestion_sets"]:
            entry = dict(entry)
            if entry.get("set_id") == set_id:
                if created_at is None:
                    entry.pop("created_at", None)
                else:
                    entry["created_at"] = created_at
            ring.append(entry)
        state["suggestion_sets"] = ring
        thread.state = state
        db.commit()


def render_stamps(seeded) -> dict[str, str]:  # noqa: ANN001
    with sync_session() as db:
        variant = db.get(Job, seeded.job_id).assembly_plan["variants"][0]
        return {k: variant[k] for k in ("render_started_at", "render_finished_at") if k in variant}


@pytest.mark.asyncio
async def test_f5_a_card_served_before_the_saves_render_finished_is_refused(talking_world):
    seeded, phone, _first = await served(talking_world)
    native_save(seeded)
    out = await phone.serve()
    card = card_of(out, "filler_words")
    stamps = render_stamps(seeded)
    # The set says it was written one microsecond after the render began, long before it ended.
    started = datetime.fromisoformat(stamps["render_started_at"].replace("Z", "+00:00"))
    set_entry_time(seeded, out.set_id, started.replace(tzinfo=UTC).isoformat())
    h.edit_variant(
        seeded, lambda v: v.update(render_finished_at=_stamp())
    )  # the render finished after the serve

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["set_time", "render_stamps"])
async def test_f6_an_unreadable_instant_falls_back_to_the_pointer_alone(talking_world, missing):
    seeded, phone, _first = await served(talking_world)
    native_save(seeded)
    out = await phone.serve()
    card = card_of(out, "filler_words")
    if missing == "set_time":
        set_entry_time(seeded, out.set_id, None)
    else:
        h.edit_variant(
            seeded,
            lambda v: (v.pop("render_started_at", None), v.pop("render_finished_at", None)),
        )

    tap = await phone.tap(card, out)

    assert (tap.refused or {}).get("reason") == "base_moved", tap.refused
    assert untouched(tap)


@pytest.mark.asyncio
async def test_f7_an_editor_op_card_is_built_on_the_saved_video_and_still_applies(world):
    seeded, phone, out = await served(world, EDITOR)
    card = card_of(out, "transition")
    generation = native_save(seeded, text_elements=[{**h.HOOK, "text": "Saved by hand"}])

    tap = await phone.tap(card, out)

    assert tap.refused is None, tap.refused
    assert tap.planned and tap.new_drafts == 1
    assert d.head_of(seeded.thread_id)["gen"] == generation
