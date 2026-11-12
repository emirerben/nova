"""Real-Postgres proof of the rule: the server moves a draft head only when it KNOWS what
the generation move did; every other move leaves the head on the old generation, as ``main``
does, and readers ignore it.

Each test is a sequence from ``docs/reviews/latest-wins-convergence/server-failure-modes.md``.
Heads are built through the real draft-turn path and generations are moved by the real
writers (approval claim, ``_complete_device_export``, the editor-commit route, the speech-cut
publication). Assertions are rows and returned documents.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid

import pytest

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import read_or_bootstrap_draft, undo_draft
from app.kria.editor_head import draft_still_current
from app.kria.runtime import RuntimeFailure
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.editor_empty_drafts import editor_sections
from app.services.kria_editor_ops import resolve_editor_base
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn
from tests.kria.test_editor_head_generation_postgres import (
    _chat_head,
    _flat,
    _phone_head,
    _timeline,
)
from tests.kria.test_editor_head_writers_postgres import (
    EDIT_HOOK,
    HOOK,
    VARIANT,
    _approved_ai_edit,
    _clear_user_override,
    _client_for,
    _head,
    _head_is_current,
    _publish_device_export,
    _rows,
    _seed,
    _variant,
    _variant_of,
)
from tests.kria.test_editor_state_turns import _IOS_CLEAN_LANES
from tests.kria.test_editor_state_turns_postgres import _plan_with, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

A, B = "AI hook", "Creator Save"


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


def _set_variant(job_id, **changes) -> None:  # noqa: ANN001, ANN003
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0].update(changes)
        job.assembly_plan = plan
        db.commit()


def _pin(job_id, draft_id) -> None:  # noqa: ANN001
    _set_variant(
        job_id,
        editor_state_version=2,
        editor_timeline_mode="authored",
        editor_draft={"draft_id": str(draft_id)},
    )


def _speech_cut_publication(job_id, monkeypatch, generation: str = "generation-cut") -> None:  # noqa: ANN001
    """The real speech-cut publication: it moves the generation and rewrites the slots."""
    from app.tasks import generative_build as gb

    operation_id, attempt_id = "op-1", "attempt-1"
    control = {
        "operation_id": operation_id,
        "variant_id": VARIANT,
        "render_generation_id": generation,
        "desired_disabled": True,
        "forced_removals": [],
        "operation": {"operation": "apply_speech_cut_candidate", "candidate_id": "cut-1"},
        "finalizer_claim": {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "render_generation_id": generation,
        },
    }
    cut = _timeline()
    cut["slots"] = [{**cut["slots"][0], "duration_s": 1.5}]
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0]["render_generation_id"] = generation
        plan["variants"][0]["ai_timeline"] = cut
        plan["variants"][0]["silence_cut"] = {"removed": []}
        plan["speech_cut_control"] = control
        job.assembly_plan = plan
        db.commit()
    monkeypatch.setattr(
        gb, "_reconcile_retired_variant_posters_after_terminal_commit", lambda *_a, **_k: None
    )
    gb._publish_speech_cut_rerender(
        str(job_id), expected_operation_id=operation_id, expected_attempt_id=attempt_id
    )


async def _mix_turn(monkeypatch, user_id, thread_id, level: float) -> None:  # noqa: ANN001
    """An AI draft turn that changes only the music level (nothing the render must re-source)."""
    async with AsyncSessionLocal() as db:
        await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    _plan_with(monkeypatch, [{"op": "set_mix", "music_level": level}])
    accepted = await _submit(user_id, thread_id, "Music")
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result


# --- 1. deploy safety: a Save over an AI head -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["legacy", "v2"])
async def test_save_over_an_ai_head_never_serves_the_ai_text_as_current(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, A)
        staged = _head(item_id)
        body = {
            "base_generation": "generation-1",
            "editor_state_version": 1 if path == "legacy" else 2,
            "text_elements": [{**HOOK, "text": B}],
        }
        if path == "v2":
            _pin(job_id, staged["id"])
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        async with _client_for(user_id) as client:
            save = await client.post(f"{base}/editor-commit", json=body)
            assert save.status_code == 200, save.text
            saved = _variant_of(job_id)["render_generation_id"]
            assert saved != "generation-1"

            draft = (await client.get(f"/creation-threads/{thread_id}/draft")).json()
            timeline = (await client.get(f"{base}/timeline")).json()
            can_undo = draft["can_undo"]
            if path == "legacy":
                # Exactly as on main: nothing moved the head, so it is not current.
                assert draft["head_current"] is False, "no writer named this head: not current"
                assert _head(item_id)["id"] == staged["id"]
                assert not timeline.get("draft")
            else:
                assert draft["head_current"] is True
                assert timeline["draft"]["can_undo"] == can_undo
            for response in (draft, timeline):
                served = json.dumps(response)
                if response.get("head_current") or path == "v2":
                    assert A not in served, "the AI's text is never served as the current draft"

        # A new chat turn builds on the saved text, not on the AI's older edit.
        _plan_with(monkeypatch, [{"op": "set_mix", "music_level": 0.9}])
        accepted = await _submit(user_id, thread_id, "More music")
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        turn_head = _head(item_id)
        assert A not in json.dumps(turn_head["snap"]), "the turn built on the saved video"
        assert turn_head["payload_base"] == saved, "and stamped the generation it built on"
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 2. approval execution retires to a fresh bootstrap ----------------------------------

V2_EXTRAS = {
    "caption_meta": {"style": "bold", "y_frac": 0.7},
    "caption_text_color": "#FFFFFF",
    "editor_split_screen_origin": "ai",
    "voiceover_caption_style": "karaoke",
    "voiceover_bed_level": 0.4,
    "background_music_treatment": "duck",
}


async def _fresh_bootstrap(job_id, thread_id) -> dict:  # noqa: ANN001
    user_id, thread2, session2 = _seed_runtime_project()
    job2 = uuid.uuid4()
    with sync_session() as db:
        source = db.get(Job, job_id)
        state = copy.deepcopy(db.get(CreationThread, thread_id).state)
        session = db.get(CreatorAgentSession, session2, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        plan = copy.deepcopy(source.assembly_plan)
        plan["variants"][0].pop("editor_draft", None)
        db.add(
            Job(
                id=job2,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates=copy.deepcopy(source.all_candidates),
                assembly_plan=plan,
            )
        )
        db.flush()
        item.current_job_id = job2
        db.get(CreationThread, thread2).state = state
        db.commit()
    async with AsyncSessionLocal() as db:
        return (await read_or_bootstrap_draft(db, thread_id=thread2, creator_id=user_id)).snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["legacy", "v2"])
async def test_approval_retires_the_head_to_exactly_a_fresh_bootstrap(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    variant = _variant(**(V2_EXTRAS if path == "v2" else {}))
    user_id, thread_id, item_id, job_id = _seed(variant)
    try:
        if path == "v2":
            _set_variant(job_id, editor_state_version=2)
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        staged = _head(item_id)
        if path == "v2":
            _set_variant(job_id, editor_draft={"draft_id": str(staged["id"])})
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        head = _head(item_id)
        assert head["parent"] == staged["id"] and head["snap"]["restage"]["of"] == str(staged["id"])
        retired = {k: v for k, v in head["snap"].items() if k != "restage"}

        fresh = await _fresh_bootstrap(job_id, thread_id)
        if path == "legacy":
            assert retired == fresh
        else:
            with sync_session() as db:
                sections = editor_sections(db.get(Job, job_id), VARIANT)
            assert retired == {
                **fresh,
                "editor_payload": {
                    "base_generation": _variant_of(job_id)["render_generation_id"],
                    "sections": sections,
                    "editor_state_version": 2,
                },
            }
            for key in V2_EXTRAS:
                assert key in retired["editor_payload"]["sections"], key
            assert _variant_of(job_id)["editor_draft"] == {"draft_id": str(head["id"])}
        rows = _rows(item_id)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert _rows(item_id) == rows, "redelivery adds zero rows"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_approved_draft_lineage_survives_a_publish_but_not_a_save_in_between(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def still_current(approval_id) -> bool:  # noqa: ANN001
        with sync_session() as db:
            draft = db.get(CreatorEditDraft, db.get(CreatorAgentApproval, approval_id).draft_id)
            return draft_still_current(db, draft)

    published = _seed()
    saved = _seed()
    try:
        # approval -> phone publish: the approved draft is still the creator's.
        user_id, thread_id, item_id, job_id = published
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert still_current(approval_id)
        _attempt, publish = _publish_device_export(
            job_id,
            user_id,
            monkeypatch,
            base_generation=_variant_of(job_id)["render_generation_id"],
        )
        await publish()
        assert still_current(approval_id), "a publish re-ids the same document"

        # approval -> manual Save -> (the Save is newer than the approval's render)
        user_id, thread_id, item_id, job_id = saved
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert still_current(approval_id)
        async with _client_for(user_id) as client:
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": _variant_of(job_id)["render_generation_id"],
                    "editor_state_version": 1,
                    "text_elements": [{**HOOK, "text": B}],
                },
            )
            assert save.status_code == 200, save.text
        assert not still_current(approval_id), "an approval never runs on top of a manual Save"
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 3. phone publish restamps only a head on the published source ----------------------


@pytest.mark.asyncio
async def test_phone_publish_restamps_a_head_on_its_source_and_leaves_any_other_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_source = _seed()
    elsewhere = _seed()
    try:
        user_id, thread_id, item_id, job_id = on_source
        await _chat_head(monkeypatch, user_id, thread_id, A)
        staged = _head(item_id)
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)
        await publish()
        head = _head(item_id)
        assert head["parent"] == staged["id"] and head["col"] == head["payload_base"] == attempt
        expected = copy.deepcopy(staged["snap"])
        expected["editor_payload"]["base_generation"] = attempt
        assert {k: v for k, v in head["snap"].items() if k != "restage"} == expected
        assert _head_is_current(item_id, job_id)
        rows = _rows(item_id)
        await publish()
        assert _rows(item_id) == rows

        # A head on a generation the publish was not made from: nothing moves it.
        user_id, thread_id, item_id, job_id = elsewhere
        await _chat_head(monkeypatch, user_id, thread_id, A)
        staged = _head(item_id)
        _speech_cut_publication(job_id, monkeypatch)  # a writer that names no head operation
        cut = _variant_of(job_id)["render_generation_id"]
        _attempt, publish = _publish_device_export(
            job_id, user_id, monkeypatch, base_generation=cut
        )
        await publish()
        assert _head(item_id)["id"] == staged["id"], "the head is not on the published source"
        assert not _head_is_current(item_id, job_id)
    finally:
        await async_engine.dispose()


# --- 4. a discard survives a restamp -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_discard_of_the_chat_draft_survives_a_phone_publish_restamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.api_schemas import EditorStateIn

    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, A)
        seen = _head(item_id)["rev"]
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)
        await publish()
        head = _head(item_id)
        assert head["rev"] > seen and _flat(head) == _flat(_rows(item_id)[-2])
        state = EditorStateIn.model_validate(
            {
                "base_generation": attempt,
                "client_state_id": "cs-clean",
                "lanes": _IOS_CLEAN_LANES,
                "draft_revision": seen,
            }
        )
        with sync_session() as db:
            job = db.get(Job, job_id)
            row = db.get(CreatorEditDraft, head["id"])
            base = resolve_editor_base(job, job.assembly_plan["variants"][0], row, state)
        assert base.source == "client_state", f"the creator discarded revision {seen}"
    finally:
        await async_engine.dispose()


# --- 5. undo ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_works_across_a_restamp_and_is_unavailable_across_an_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restamped = _seed()
    approved = _seed()
    try:
        user_id, thread_id, item_id, job_id = restamped
        await _chat_head(monkeypatch, user_id, thread_id, A)
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)
        await publish()
        head = _head(item_id)
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert served.can_undo is True
        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"]
            )
        assert undone.head_current is True and undone.base_generation == attempt
        assert A not in json.dumps(undone.snapshot)
        assert undone.snapshot["editor_payload"]["sections"]["text_elements"][0]["text"] == (
            "Old matcha hook"
        )

        # Two chat turns, then the approval of the second renders: the render holds both.
        user_id, thread_id, item_id, job_id = approved
        await _mix_turn(monkeypatch, user_id, thread_id, 0.2)
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        head = _head(item_id)
        rows = _rows(item_id)
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert served.can_undo is False
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as raised:
                await undo_draft(
                    db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"]
                )
        assert raised.value.code == "draft_undo_unavailable"
        assert _rows(item_id) == rows
        with sync_session() as db:
            assert db.get(CreatorAgentApproval, approval_id).status == "consumed"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_can_undo_is_false_on_the_timeline_read_too_after_an_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _mix_turn(monkeypatch, user_id, thread_id, 0.2)
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        _pin(job_id, _head(item_id)["id"])
        async with _client_for(user_id) as client:
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert timeline.status_code == 200, timeline.text
            draft = await client.get(f"/creation-threads/{thread_id}/draft")
        assert timeline.json()["draft"]["can_undo"] is False
        assert draft.json()["can_undo"] is False
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 6. a stranded pinned draft -----------------------------------------------------------


async def _stranded_pinned(monkeypatch, *, empty: bool):  # noqa: ANN001, ANN202
    variant = _variant(**V2_EXTRAS)
    user_id, thread_id, item_id, job_id = _seed(variant)
    staged = await _phone_head(user_id, thread_id, "Pinned hook")
    changes = {"editor_draft": {"draft_id": staged.draft_id}}
    changes.update(
        editor_state="empty" if empty else None,
        editor_state_version=2,
        editor_timeline_mode="authored",
    )
    _set_variant(job_id, **{k: v for k, v in changes.items() if v is not None})
    _set_variant(job_id, render_generation_id="generation-2")  # a dispatcher moves it
    return user_id, thread_id, item_id, job_id, staged


@pytest.mark.asyncio
async def test_stranded_pinned_v2_draft_timeline_is_the_committed_state_and_save_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(monkeypatch, empty=False)
    try:
        rows = _rows(item_id)
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        async with _client_for(user_id) as client:
            timeline = await client.get(f"{base}/timeline")
            assert timeline.status_code == 200, timeline.text
            served = timeline.json()["draft"]
            with sync_session() as db:
                committed = editor_sections(db.get(Job, job_id), VARIANT)
            assert served["snapshot"]["editor_payload"]["sections"] == committed
            for key in V2_EXTRAS:
                assert key in served["snapshot"]["editor_payload"]["sections"], key
            assert _rows(item_id) == rows, "the read writes nothing"

            save = await client.post(
                f"{base}/editor-commit",
                json={
                    "base_generation": "generation-2",
                    "editor_state_version": 2,
                    "text_elements": [{**HOOK, "text": B}],
                },
            )
            assert save.status_code == 200, save.text
        assert _head_is_current(item_id, job_id)
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_stranded_empty_state_timeline_is_unchanged_from_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main: the empty variant's content exists only in its draft, so an unattachable pinned
    draft answers 409 ``editor_draft_unavailable``. The server does not guess."""
    user_id, thread_id, item_id, job_id, _staged = await _stranded_pinned(monkeypatch, empty=True)
    try:
        rows = _rows(item_id)
        async with _client_for(user_id) as client:
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
        assert timeline.status_code == 409
        assert timeline.json()["detail"] == "editor_draft_unavailable"
        assert _rows(item_id) == rows
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 7. a writer that restages nothing ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_generation_move_by_the_speech_cut_leaves_the_head_not_current_and_unserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, A)
        staged = _head(item_id)
        rows = _rows(item_id)
        _speech_cut_publication(job_id, monkeypatch)
        assert _rows(item_id) == rows, "the speech cut writes no draft row"
        assert not _head_is_current(item_id, job_id)

        async with _client_for(user_id) as client:
            draft = (await client.get(f"/creation-threads/{thread_id}/draft")).json()
            assert draft["head_current"] is False and draft["draft_id"] == str(staged["id"])
        assert _rows(item_id) == rows, "a read does not heal the head"

        _plan_with(monkeypatch, [{"op": "set_mix", "music_level": 0.9}])
        accepted = await _submit(user_id, thread_id, "More music")
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        assert A not in json.dumps(_head(item_id)["snap"]), "the turn built on the cut video"
    finally:
        _clear_user_override()
        await async_engine.dispose()
