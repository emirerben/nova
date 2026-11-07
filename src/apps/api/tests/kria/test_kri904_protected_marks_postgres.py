"""KRI-904: a creator's mark of what to keep text off can be withdrawn, and only a turn
that stands binds one.

Ways the marks the approval pins (`_protected_regions_sync`) can be wrong:
F1 the creator says "text on the plate is fine" (the brief requirement is removed, or
   changed to drop the plate) and the old mark still keeps text off the plate;
F2 the same withdrawal wrongly clears a mark nobody ever tied to a keep-off ask (drawing
   the box was the ask), or one a later turn asked for again;
F3 a mark sent on a turn the creator then cancelled still binds the approval;
F5 the serve's `_mark_needed` and the restate readiness (`_readiness_problem`) mirror the
   gate, yet folded every user event: a cancelled turn's mark, or a withdrawn one, counted
   there and not at the commit, so the GET served a card whose tap asked for the mark;
F4 a mark is accepted on a clip with no storage generation yet, the clip gets one when the
   upload finishes, and the gate calls the mark current while the render reads it as
   invalidated.
Needs the ``nova_test`` Postgres (CI's test-api job).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.protected_targets import ProtectedRegionSelection, missing_object_marks
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentTurn,
    PlanItem,
)
from app.tasks.kria_runtime import _protected_regions_sync
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

BOX = (0.3, 0.42, 0.7, 0.64)
KEEP_PLATE = {"keep_text_off": [{"kind": "object", "label": "plate"}]}


def _mark(label: str, generation: str | None = "g7") -> dict:
    return {
        "label": label,
        "media_id": "clip-1",
        "source_t_s": 1.5,
        "box": list(BOX),
        "generation": generation,
    }


def _add_event(thread_id: uuid.UUID, sequence: int, payload: dict, *, turn: str | None) -> None:
    with sync_session() as db:
        event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=sequence,
            revision=100 + sequence,
            role="user",
            event_type="message",
            content="x",
            payload=payload,
        )
        db.add(event)
        db.flush()
        if turn is not None:
            db.add(
                CreatorAgentTurn(
                    thread_id=thread_id,
                    source_event_id=event.id,
                    client_event_id=f"c-{sequence}",
                    request_digest="d",
                    status=turn,
                )
            )
        db.commit()


def _store_brief(thread_id: uuid.UUID, version: int, brief: CreativeBrief) -> None:
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=version,
                requirements=[r.model_dump(mode="json") for r in brief.requirements],
            )
        )
        db.commit()


def _keep_off(facts: dict, *, brief: CreativeBrief | None = None, version: int = 0):
    update = BriefUpdate(
        operation="add", kind="style", scope="global", description="keep text off", facts=facts
    )
    return apply_updates(brief, [update], source_turn_id=None).model_copy(
        update={"version": version + 1}
    )


def _marks(thread_id: uuid.UUID, through: int = 50) -> list[str]:
    with sync_session() as db:
        return [
            m["label"]
            for m in _protected_regions_sync(db, thread_id=thread_id, through_sequence=through)
        ]


def test_a_mark_from_a_cancelled_turn_does_not_bind() -> None:
    _, thread_id, _ = _seed_runtime_project()
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="cancelled")
    _add_event(thread_id, 11, {"protected_region": _mark("cup")}, turn="completed")
    assert _marks(thread_id) == ["cup"]


def test_removing_the_keep_off_ask_withdraws_its_mark() -> None:
    _, thread_id, _ = _seed_runtime_project()
    v1 = _keep_off(KEEP_PLATE)
    _store_brief(thread_id, 1, v1)
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="completed")
    assert _marks(thread_id) == ["plate"]
    v2 = apply_updates(
        v1,
        [BriefUpdate(operation="remove", target_requirement_id="r1", expected_version=1)],
        source_turn_id=None,
    ).model_copy(update={"version": 2})
    _store_brief(thread_id, 2, v2)
    assert _marks(thread_id) == []


def test_changing_the_ask_to_drop_the_plate_withdraws_its_mark() -> None:
    _, thread_id, _ = _seed_runtime_project()
    v1 = _keep_off({"keep_text_off": [{"kind": "face"}, *KEEP_PLATE["keep_text_off"]]})
    _store_brief(thread_id, 1, v1)
    _add_event(thread_id, 10, {"protected_region": _mark("Plate")}, turn="completed")
    change = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=1,
        kind="style",
        scope="global",
        description="no text on her face",
        facts={"keep_text_off": [{"kind": "face"}]},
    )
    v2 = apply_updates(v1, [change], source_turn_id=None).model_copy(update={"version": 2})
    _store_brief(thread_id, 2, v2)
    assert _marks(thread_id) == []


def test_a_change_that_keeps_the_plate_keeps_its_mark() -> None:
    _, thread_id, _ = _seed_runtime_project()
    v1 = _keep_off(KEEP_PLATE)
    _store_brief(thread_id, 1, v1)
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="completed")
    change = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=1,
        kind="style",
        scope="global",
        description="no text on the plate or the cup",
        facts={"keep_text_off": [*KEEP_PLATE["keep_text_off"], {"kind": "object", "label": "cup"}]},
    )
    v2 = apply_updates(v1, [change], source_turn_id=None).model_copy(update={"version": 2})
    _store_brief(thread_id, 2, v2)
    assert _marks(thread_id) == ["plate"]


def test_a_mark_no_ask_ever_named_still_protects() -> None:
    """Drawing the box was the ask: a brief that never named the object withdraws nothing."""
    _, thread_id, _ = _seed_runtime_project()
    _store_brief(thread_id, 1, _keep_off({"keep_text_off": [{"kind": "face"}]}))
    _add_event(thread_id, 10, {"protected_region": _mark("cup")}, turn="completed")
    assert _marks(thread_id) == ["cup"]


def test_asking_for_the_plate_again_after_withdrawing_it_protects_again() -> None:
    _, thread_id, _ = _seed_runtime_project()
    v1 = _keep_off(KEEP_PLATE)
    _store_brief(thread_id, 1, v1)
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="completed")
    v2 = apply_updates(
        v1,
        [BriefUpdate(operation="remove", target_requirement_id="r1", expected_version=1)],
        source_turn_id=None,
    ).model_copy(update={"version": 2})
    _store_brief(thread_id, 2, v2)
    v3 = _keep_off(KEEP_PLATE, brief=v2, version=2)
    _store_brief(thread_id, 3, v3)
    assert _marks(thread_id) == ["plate"]


def test_a_legacy_mark_without_a_generation_is_asked_for_again_once_the_clip_has_one() -> None:
    """The gate must agree with the render, which reads a mark whose generation differs
    from the clip's as invalidated."""
    brief = _keep_off(KEEP_PLATE)
    legacy = ProtectedRegionSelection.model_validate(_mark("plate", generation=None))
    missing = missing_object_marks(brief, [legacy], {"clip-1": "g7"}, render_clips=["clip-1"])
    assert [(t.label, clips) for t, clips in missing] == [("plate", ("clip-1",))]
    assert missing_object_marks(brief, [legacy], {"clip-1": None}, render_clips=["clip-1"]) == ()


@pytest.mark.asyncio
async def test_a_mark_on_a_clip_still_uploading_is_refused_with_a_retry() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_assignments = [{"media_id": "clip-1"}]
        db.commit()
        revision = thread.revision
    body = SubmitTurnBody(
        message="This is the plate",
        client_event_id=f"mark-{uuid.uuid4().hex}",
        expected_thread_revision=revision,
        protected_region={"label": "plate", "media_id": "clip-1", "source_t_s": 1.5, "box": BOX},
    )
    try:
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
        assert (caught.value.status_code, caught.value.recovery, caught.value.retryable) == (
            409,
            "retry",
            True,
        )
        assert caught.value.code == "protected_region_clip_uploading"
    finally:
        await async_engine.dispose()


def _restate_readiness_marks(monkeypatch, thread_id: uuid.UUID) -> list[str]:
    """The marks the restate readiness hands ``assess_readiness`` (the serve's twin of the
    commit's readiness, `_protected_regions_sync`)."""
    from app.kria import approval_readiness, order_replacement_receipts, order_replacement_sources
    from app.services import phone_montage_preflight
    from app.services.suggestions import restate_judgment

    seen: list[list[str]] = []

    def assess(*_args, protected_regions, **_kwargs):  # noqa: ANN202
        seen.append([m["label"] for m in protected_regions])
        return {"requirements": [], "ready": True}

    monkeypatch.setattr(approval_readiness, "assess_readiness", assess)
    monkeypatch.setattr(
        order_replacement_receipts, "compiled_order_preflight_sync", lambda *a, **k: None
    )
    monkeypatch.setattr(
        order_replacement_sources, "load_brief_order_source_context_sync", lambda *a, **k: None
    )
    monkeypatch.setattr(phone_montage_preflight, "readiness_plan_blockers", lambda *a, **k: ())
    with sync_session() as db:
        restate_judgment._readiness_problem(
            db,
            thread=SimpleNamespace(id=thread_id, creator_id=1),
            item=SimpleNamespace(
                id=uuid.uuid4(), edit_format=None, clip_gcs_paths=(), voiceover_gcs_path=None
            ),
            brief=None,
            strategy={},
            media={},
            manifest=None,
            client_protocol=None,
        )
    return seen[0]


def test_the_restate_readiness_ignores_a_mark_from_a_cancelled_turn(monkeypatch) -> None:
    _, thread_id, _ = _seed_runtime_project()
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="cancelled")
    _add_event(thread_id, 11, {"protected_region": _mark("cup")}, turn="completed")
    assert _restate_readiness_marks(monkeypatch, thread_id) == ["cup"]


def test_the_restate_readiness_ignores_a_withdrawn_mark(monkeypatch) -> None:
    _, thread_id, _ = _seed_runtime_project()
    v1 = _keep_off(KEEP_PLATE)
    _store_brief(thread_id, 1, v1)
    _add_event(thread_id, 10, {"protected_region": _mark("plate")}, turn="completed")
    v2 = apply_updates(
        v1,
        [BriefUpdate(operation="remove", target_requirement_id="r1", expected_version=1)],
        source_turn_id=None,
    ).model_copy(update={"version": 2})
    _store_brief(thread_id, 2, v2)
    assert _restate_readiness_marks(monkeypatch, thread_id) == []
