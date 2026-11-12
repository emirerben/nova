"""Real-Postgres proof of the second review round (``server-failure-modes.md``, "Review round 2").

Every sequence runs through the real writers: the editor-commit route (a v2 Save pins its own
draft), the speech-cut publication, a real chat turn, the phone publish, ``write_draft`` and
``undo_draft``. Assertions are rows and returned documents.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.editor_head import is_head_current
from app.models import CreatorEditDraft, Job
from tests.kria.test_editor_head_deploy_safety_postgres import (
    _speech_cut_publication,
)
from tests.kria.test_editor_head_generation_postgres import _chat_head
from tests.kria.test_editor_head_writers_postgres import (
    HOOK,
    VARIANT,
    _clear_user_override,
    _client_for,
    _head,
    _publish_device_export,
    _rows,
    _seed,
    _variant,
    _variant_of,
)

AI_TITLE, SAVED_TITLE = "AI title nobody saved", "Saved by hand"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.routes.test_editor_commit import _arm

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    _arm(monkeypatch)


def _pinned(job_id) -> str | None:  # noqa: ANN001
    return ((_variant_of(job_id).get("editor_draft")) or {}).get("draft_id")


def _kind_of_pinned_row(job_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        row = db.get(CreatorEditDraft, _pinned(job_id))
        return {"restage": (row.snapshot_json or {}).get("restage"), "snap": row.snapshot_json}


async def _v2_save(client, item_id, job_id, *, text: str) -> None:  # noqa: ANN001
    base = _variant_of(job_id)["render_generation_id"]
    response = await client.post(
        f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
        json={
            "base_generation": base,
            "editor_state_version": 2,
            "text_elements": [{**HOOK, "text": text}],
        },
    )
    assert response.status_code == 200, response.text


async def _mirror_ai_title_into_sections(user_id, thread_id, job_id) -> None:  # noqa: ANN001
    """The phone applies the chat head's lanes and writes its whole ``sections`` object back."""
    from app.services.editor_empty_drafts import editor_sections

    async with AsyncSessionLocal() as db:
        served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    with sync_session() as db:
        sections = copy.deepcopy(editor_sections(db.get(Job, job_id), VARIANT))
    sections["text_elements"][0]["text"] = AI_TITLE
    payload = {**served.snapshot["editor_payload"], "sections": sections}
    async with AsyncSessionLocal() as db:
        await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=served.draft_revision,
            expected_etag=served.etag,
            snapshot={**served.snapshot, "editor_payload": payload},
        )


# --- 1. the pin follows only its own row ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_save_after_a_speech_cut_and_a_chat_turn_never_commits_the_unsaved_ai_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(editor_timeline_mode="authored"))
    try:
        async with _client_for(user_id) as client:
            await _v2_save(client, item_id, job_id, text=SAVED_TITLE)
            pinned = _pinned(job_id)
            assert pinned is not None and pinned == str(_head(item_id)["id"])

            _speech_cut_publication(job_id, monkeypatch)  # moves the generation, restages nothing
            await _chat_head(monkeypatch, user_id, thread_id, AI_TITLE)
            await _mirror_ai_title_into_sections(user_id, thread_id, job_id)
            chat_head = _head(item_id)
            assert str(chat_head["id"]) != pinned
            assert AI_TITLE in json.dumps(chat_head["snap"]["editor_payload"]["sections"])

            # The creator trims and Saves without the title.
            variant = _variant_of(job_id)
            slots = [
                {**row, "duration_s": 2.5} if row["slot_id"] == "s0" else row
                for row in variant["ai_timeline"]["slots"]
            ]
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": variant["render_generation_id"],
                    "editor_state_version": 2,
                    "timeline_slots": [
                        {
                            "slot_id": row["slot_id"],
                            "clip_index": row["clip_index"],
                            "in_s": row["in_s"],
                            "duration_s": row["duration_s"],
                        }
                        for row in slots
                    ],
                },
            )
            assert save.status_code == 200, save.text

            committed = _variant_of(job_id)
            assert AI_TITLE not in json.dumps(committed.get("text_elements")), "committed"
            assert _pinned(job_id) != str(chat_head["id"]), "the pin never names a chat draft"
            assert AI_TITLE not in json.dumps(_kind_of_pinned_row(job_id)["snap"])
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert timeline.status_code == 200, timeline.text
            assert AI_TITLE not in json.dumps(timeline.json())
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_phone_publish_never_moves_the_pin_from_the_saved_draft_to_a_chat_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(editor_timeline_mode="authored"))
    try:
        async with _client_for(user_id) as client:
            await _v2_save(client, item_id, job_id, text=SAVED_TITLE)
        pinned = _pinned(job_id)
        saved_generation = _variant_of(job_id)["render_generation_id"]
        await _chat_head(monkeypatch, user_id, thread_id, AI_TITLE)
        chat_head = _head(item_id)
        assert str(chat_head["id"]) != pinned
        assert chat_head["col"] == saved_generation, "pinned X and chat head H share a generation"

        attempt, publish = _publish_device_export(
            job_id, user_id, monkeypatch, base_generation=saved_generation
        )
        await publish()

        assert _variant_of(job_id)["render_generation_id"] == attempt
        assert _pinned(job_id) == pinned, "the saved draft stays the pin"
        restaged = _head(item_id)
        assert restaged["snap"]["restage"]["reason"] == "phone_publish"
        assert AI_TITLE not in json.dumps(_kind_of_pinned_row(job_id)["snap"])
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 2. undo across a restamp --------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_across_a_restamp_never_stamps_an_older_generations_lanes_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        # P: a creator draft on generation-1 (the bootstrap), then a manual Save moves to G2.
        async with AsyncSessionLocal() as db:
            parent = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert parent.base_generation == "generation-1"
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0]["render_generation_id"] = "generation-2"
            plan["variants"][0]["text_elements"] = [{**HOOK, "text": "Saved v2"}]
            job.assembly_plan = plan
            db.commit()
        # A: a chat turn on generation-2 whose parent is P (a head on an older generation).
        await _chat_head(monkeypatch, user_id, thread_id, AI_TITLE)
        chat = _head(item_id)
        assert str(chat["parent"]) == parent.draft_id and chat["col"] == "generation-2"

        attempt, publish = _publish_device_export(
            job_id, user_id, monkeypatch, base_generation="generation-2"
        )
        await publish()
        restamped = _head(item_id)
        assert restamped["snap"]["restage"]["reason"] == "phone_publish"

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=restamped["rev"]
            )

        restored = _head(item_id)
        # P is restored exactly as it was: its generation-1 lanes are not current.
        assert restored["payload_base"] == "generation-1"
        assert undone.base_generation == "generation-1"
        assert undone.head_current is False
        with sync_session() as db:
            job = db.get(Job, job_id)
            head = db.get(CreatorEditDraft, restored["id"])
            assert not is_head_current(job, job.assembly_plan["variants"][0], head)
    finally:
        await async_engine.dispose()


# --- 3. restage is server-owned ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_put_echoing_a_restamped_snapshot_writes_a_creator_row_with_no_restage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, AI_TITLE)
        creator_row = _head(item_id)
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)
        await publish()
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert served.snapshot["restage"]["reason"] == "phone_publish"

        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=served.draft_revision,
                expected_etag=served.etag,
                snapshot=copy.deepcopy(served.snapshot),  # echoes ``restage`` back
            )
        assert "restage" not in written.snapshot
        assert "restage" not in _head(item_id)["snap"]

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=written.draft_revision,
            )
        # Undo targets the creator's PUT (the restamp row it wrote over), not the row before it.
        assert undone.snapshot["editor_payload"]["base_generation"] == attempt
        assert str(_rows(item_id)[-1]["parent"]) == written.draft_id
        assert creator_row["id"] is not None
    finally:
        await async_engine.dispose()


# --- 5. one can_undo -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_plan_feed_reports_the_same_can_undo_as_the_draft_after_an_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria import plan_blocks
    from app.kria.plan_contract import SECTION_ORDER
    from app.kria.plan_snapshot import read_plan_snapshot
    from app.tasks.kria_runtime import execute_kria_approval
    from tests.kria.test_editor_head_deploy_safety_postgres import _mix_turn
    from tests.kria.test_editor_head_writers_postgres import EDIT_HOOK, _approved_ai_edit

    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _mix_turn(monkeypatch, user_id, thread_id, 0.2)
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        plan_blocks.emit_plan_blocks(
            job_id, [plan_blocks.block(SECTION_ORDER[0], "decided", "Not used", skipped=True)]
        )
        async with AsyncSessionLocal() as db:
            draft = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
            feed = await read_plan_snapshot(db, thread_id=thread_id, creator_id=user_id)
        assert draft.can_undo is False, "committed work is not undone by draft undo"
        assert feed.draft is not None and feed.draft.can_undo is draft.can_undo
    finally:
        await async_engine.dispose()


# --- 6. rows main wrote for a variant with only render_finished_at -------------------------


@pytest.mark.asyncio
async def test_a_main_bootstrapped_head_of_a_finished_at_only_variant_keeps_mains_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head ``main`` bootstraps for such a variant has column None and payload "".

    main: the planner ignores it (payload "" is not the baseline), Draft PUT accepts it (column
    None is the target's None) and the saved-draft attach accepts it (None equals the variant's
    absent ``render_generation_id``). Each answer is unchanged here.
    """
    from app.kria.drafts import _variant as draft_variant
    from app.services.editor_empty_drafts import attach_saved_editor_drafts
    from app.services.kria_editor_ops import fresh_editor_head_payload

    user_id, thread_id, item_id, job_id = _seed(_variant(None))
    try:
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        with sync_session() as db:
            row = db.get(CreatorEditDraft, served.draft_id)
            snapshot = copy.deepcopy(row.snapshot_json)
            snapshot["editor_payload"]["base_generation"] = ""
            row.snapshot_json = snapshot
            row.base_generation_id = None
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0].update(
                editor_state_version=2, editor_draft={"draft_id": served.draft_id}
            )
            job.assembly_plan = plan
            db.commit()

        with sync_session() as db:
            job, head = db.get(Job, job_id), db.get(CreatorEditDraft, served.draft_id)
            variant = draft_variant(job, VARIANT)
            assert fresh_editor_head_payload(head, variant, job) == {}, "planner: ignored"
        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            await attach_saved_editor_drafts(db, job)
            attached = job._saved_editor_drafts
        assert VARIANT in attached, "attach: accepted"
        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=served.draft_revision,
                expected_etag=served.etag,
                snapshot=copy.deepcopy(served.snapshot) | {"changes": ["Phone edit"]},
            )
        assert written.draft_revision == served.draft_revision + 1, "PUT: accepted"
    finally:
        await async_engine.dispose()


# --- 7. an unrelated stranded variant never fails the timeline read ------------------------


@pytest.mark.asyncio
async def test_the_timeline_read_survives_another_variants_unbuildable_committed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models import CreatorEditDraft as Draft
    from tests.kria.test_editor_head_deploy_safety_postgres import _stranded_pinned

    other = "song_text"
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(monkeypatch, empty=False)
    try:
        with sync_session() as db:
            source = db.get(Draft, staged.draft_id)
            twin = Draft(
                creator_id=source.creator_id,
                thread_id=source.thread_id,
                item_id=source.item_id,
                variant_key=other,
                base_job_id=source.base_job_id,
                base_generation_id=source.base_generation_id,
                draft_revision=0,
                snapshot_json=copy.deepcopy(source.snapshot_json),
                snapshot_hash=source.snapshot_hash,
                is_head=True,
            )
            db.add(twin)
            db.flush()
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            second = copy.deepcopy(plan["variants"][0])
            second.update(variant_id=other, editor_draft={"draft_id": str(twin.id)})
            plan["variants"].append(second)
            job.assembly_plan = plan
            db.commit()

        from app.services import editor_empty_drafts as module

        real = module.committed_document

        def broken_for_other(*, variant_key, **kwargs):  # noqa: ANN003, ANN202
            if variant_key == other:
                raise ValueError("cannot project this variant")
            return real(variant_key=variant_key, **kwargs)

        monkeypatch.setattr(module, "committed_document", broken_for_other)
        base = f"/plan-items/{item_id}/variants"
        async with _client_for(user_id) as client:
            requested = await client.get(f"{base}/{VARIANT}/timeline")
            assert requested.status_code == 200, "the other variant is skipped, as on main"
            assert requested.json()["draft"] is not None
            with pytest.raises(ValueError, match="cannot project"):
                await client.get(f"{base}/{other}/timeline")  # its own failure is not hidden
    finally:
        _clear_user_override()
        await async_engine.dispose()
