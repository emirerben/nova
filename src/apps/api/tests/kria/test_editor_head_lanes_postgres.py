"""Real-Postgres proof of the per-lane rebase of an editor draft head.

Failure modes 51-61 of ``docs/reviews/latest-wins-convergence/server-failure-modes.md``
("Lane-level rebase"). Each test is one sequence found by the independent review of the
whole-content rebase, driven through the real code path, asserting rows and documents.

Two head shapes exist and both are exercised: a FULL-``sections`` head (a bootstrapped or
phone-written head stores every lane under ``sections``) and a FLAT-lane chat head (the draft
turn stores only the lanes the edit changed at the top level of the payload, which is the shape
``_complete_draft_turn`` builds and ``project_editor_draft`` reads).
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.editor_head import draft_still_current
from app.kria.runtime import RuntimeFailure
from app.models import CreatorAgentApproval, CreatorEditDraft, Job
from app.tasks.kria_runtime import _claim_approval_dispatch, execute_kria_approval
from tests.kria.test_editor_head_generation_postgres import _update_variant
from tests.kria.test_editor_head_writers_postgres import (
    EDIT_HOOK,
    HOOK,
    VARIANT,
    _approved_ai_edit,
    _clear_user_override,
    _client_for,
    _head,
    _publish_device_export,
    _rows,
    _seed,
    _stage_ai_edit,
    _variant,
    _variant_of,
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _lane(payload: dict, key: str):  # noqa: ANN001, ANN202
    """A lane as the phone reads it: the flat value wins, ``sections`` fills the rest."""
    if key in payload:
        return payload[key]
    return (payload.get("sections") or {}).get(key)


def _texts(row: dict) -> list[str]:
    return [bar["text"] for bar in _lane(row["snap"]["editor_payload"], "text_elements")]


async def _flat_chat_head(user_id, thread_id, **lanes):  # noqa: ANN001, ANN003, ANN202
    """A chat head shaped like ``_complete_draft_turn`` builds it: flat lanes, no ``sections``."""
    async with AsyncSessionLocal() as db:
        initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    generation = initial.snapshot["editor_payload"]["base_generation"]
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=initial.draft_revision,
            expected_etag=initial.etag,
            snapshot={
                **initial.snapshot,
                "editor_payload": {"base_generation": generation, **lanes},
                "changes": ["AI edit"],
            },
        )


def _strip_base_lanes(item_id) -> None:  # noqa: ANN001
    """Make every row look like one written before the change (no recorded base)."""
    with sync_session() as db:
        db.execute(
            text(
                "UPDATE creator_edit_drafts SET snapshot_json = "
                "snapshot_json #- '{editor_payload,base_lanes}' WHERE item_id = :item"
            ),
            {"item": item_id},
        )
        db.commit()


async def _read(user_id, thread_id):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        return await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)


SLOTS_BEFORE = [
    {"slot_id": "s1", "clip_index": 0, "output_start_s": 0.0, "output_end_s": 3.0},
    {"slot_id": "s2", "clip_index": 1, "output_start_s": 3.0, "output_end_s": 6.0},
]
SLOTS_CUT = [{"slot_id": "s1", "clip_index": 0, "output_start_s": 0.0, "output_end_s": 2.0}]


# --- 51: a legacy dispatcher under a full-sections head ---------------------------------


@pytest.mark.asyncio
async def test_legacy_music_swap_under_a_full_sections_head_survives_the_heal() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")

        healed = await _read(user_id, thread_id)

        sections = healed.snapshot["editor_payload"]["sections"]
        assert sections["music_track_id"] == "song-B", "the dispatcher's music is the latest"
        assert sections["text_elements"][0]["text"] == "AI hook", "the unsaved AI edit stays"
        assert healed.head_current is True
        assert healed.snapshot["changes"] == ["Retitle the hook"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_written_before_the_change_takes_committed_on_sections_only_lanes() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _strip_base_lanes(item_id)
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")

        healed = await _read(user_id, thread_id)

        sections = healed.snapshot["editor_payload"]["sections"]
        assert sections["music_track_id"] == "song-B"
        assert healed.snapshot["editor_payload"].get("base_lanes"), "the new row records its base"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_unrecorded_flat_chat_head_keeps_its_lanes_through_a_legacy_bump() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        await _flat_chat_head(user_id, thread_id, text_elements=[{**HOOK, "text": "AI hook"}])
        _strip_base_lanes(item_id)
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")

        healed = await _read(user_id, thread_id)

        payload = healed.snapshot["editor_payload"]
        assert _lane(payload, "text_elements")[0]["text"] == "AI hook"
        assert healed.snapshot["changes"] == ["AI edit"]
    finally:
        await async_engine.dispose()


# --- 52: a speech cut under a flat-lane chat head ---------------------------------------


@pytest.mark.asyncio
async def test_speech_cut_under_a_flat_lane_chat_head_keeps_the_cut_and_the_text_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb

    operation_id, attempt_id, cut_generation = "op-1", "attempt-1", "generation-cut"
    control = {
        "operation_id": operation_id,
        "variant_id": VARIANT,
        "render_generation_id": cut_generation,
        "desired_disabled": True,
        "forced_removals": [],
        "operation": {"operation": "apply_speech_cut_candidate", "candidate_id": "cut-1"},
        "finalizer_claim": {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "render_generation_id": cut_generation,
        },
    }
    user_id, thread_id, item_id, job_id = _seed(
        _variant(
            "generation-1",
            user_timeline={"slots": SLOTS_BEFORE},
            silence_cut={"removed": []},
        ),
        speech_cut_control=control,
    )
    try:
        await _flat_chat_head(
            user_id,
            thread_id,
            text_elements=[{**HOOK, "text": "AI hook"}],
            timeline_slots=copy.deepcopy(SLOTS_BEFORE),
        )
        _update_variant(
            job_id, render_generation_id=cut_generation, user_timeline={"slots": SLOTS_CUT}
        )
        monkeypatch.setattr(
            gb, "_reconcile_retired_variant_posters_after_terminal_commit", lambda *_a, **_k: None
        )

        await asyncio.to_thread(
            gb._publish_speech_cut_rerender,
            str(job_id),
            expected_operation_id=operation_id,
            expected_attempt_id=attempt_id,
        )

        payload = _head(item_id)["snap"]["editor_payload"]
        assert payload["base_generation"] == cut_generation
        assert _lane(payload, "timeline_slots") in (None, SLOTS_CUT), "the cut is not undone"
        assert "timeline_slots" not in payload or payload["timeline_slots"] == SLOTS_CUT
        assert _lane(payload, "text_elements")[0]["text"] == "AI hook", "the untouched edit stays"
    finally:
        await async_engine.dispose()


# --- 53: a legacy Save over an AI text lane ---------------------------------------------


@pytest.mark.asyncio
async def test_legacy_save_over_an_ai_text_lane_leaves_the_saved_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        staged = _head(item_id)
        assert _texts(staged) == ["Fresh matcha, finally"]
        assert "sections" not in staged["snap"]["editor_payload"], "a chat draft is flat"

        async with _client_for(user_id) as client:
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": "generation-1",
                    "editor_state_version": 1,
                    "text_elements": [{**HOOK, "text": "Creator saved text"}],
                },
            )
            assert save.status_code == 200, save.text

        assert _texts(_head(item_id)) == ["Creator saved text"], "the Save is the latest"
        assert _variant_of(job_id)["text_elements"][0]["text"] == "Creator saved text"
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 54: a stale PUT /draft cannot resurrect G0 content ---------------------------------


@pytest.mark.asyncio
async def test_stale_put_after_a_legacy_dispatcher_cannot_resurrect_old_lanes() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        seen = await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")

        payload = copy.deepcopy(seen.snapshot["editor_payload"])
        payload["sections"]["text_elements"][0]["text"] = "Client typed"
        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=seen.draft_revision,
                expected_etag=seen.etag,
                snapshot={**seen.snapshot, "editor_payload": payload, "changes": ["Typed"]},
            )

        sections = written.snapshot["editor_payload"]["sections"]
        assert sections["music_track_id"] == "song-B", "old committed content is not re-asserted"
        assert sections["text_elements"][0]["text"] == "Client typed"
        assert written.head_current is True
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_stale_put_that_only_reasserts_old_content_changes_nothing() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        async with AsyncSessionLocal() as db:
            seen = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")

        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=seen.draft_revision,
                expected_etag=seen.etag,
                snapshot=copy.deepcopy(seen.snapshot),
            )

        assert written.snapshot["editor_payload"]["sections"]["music_track_id"] == "song-B"
    finally:
        await async_engine.dispose()


# --- 55: lineage of a restage -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pending_approval_across_a_manual_save_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        with sync_session() as db:
            approved = db.get(CreatorAgentApproval, approval_id)
            draft_id = approved.draft_id

        async with _client_for(user_id) as client:
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": "generation-1",
                    "editor_state_version": 1,
                    "text_elements": [{**HOOK, "text": "Creator saved text"}],
                },
            )
            assert save.status_code == 200, save.text

        with sync_session() as db:
            assert draft_still_current(db, db.get(CreatorEditDraft, draft_id)) is False
        claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
        assert claim is None, "the approval must not run on top of the creator's Save"
        assert _variant_of(job_id)["text_elements"][0]["text"] == "Creator saved text"
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_approval_stays_current_across_its_own_render_and_a_phone_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        with sync_session() as db:
            draft_id = db.get(CreatorAgentApproval, approval_id).draft_id
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        with sync_session() as db:
            assert draft_still_current(db, db.get(CreatorEditDraft, draft_id)) is True

        _attempt, publish = _publish_device_export(
            job_id,
            user_id,
            monkeypatch,
            base_generation=_variant_of(job_id)["render_generation_id"],
        )
        await publish()

        with sync_session() as db:
            assert draft_still_current(db, db.get(CreatorEditDraft, draft_id)) is True
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_restage_that_replaced_a_lane_of_the_draft_breaks_the_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone publish whose committed state changed the draft's own lane is not 'its render'."""
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        with sync_session() as db:
            draft_id = db.get(CreatorAgentApproval, approval_id).draft_id
        # A dispatcher rewrites the same text lane before anything observes the head.
        _update_variant(
            job_id,
            render_generation_id="generation-2",
            text_elements=[{**HOOK, "text": "Dispatcher text"}],
        )
        await _read(user_id, thread_id)

        with sync_session() as db:
            assert draft_still_current(db, db.get(CreatorEditDraft, draft_id)) is False
    finally:
        await async_engine.dispose()


# --- 56: undo after a restage -----------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_after_a_restage_has_nothing_to_undo_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        restaged = _head(item_id)
        rows_before = _rows(item_id)

        read = await _read(user_id, thread_id)
        assert read.can_undo is False, "the render holds the edit: nothing unsaved to undo"

        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as refused:
                await undo_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=restaged["rev"],
                )
        assert refused.value.code == "draft_undo_unavailable"
        assert _rows(item_id) == rows_before
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_of_an_unsaved_edit_after_an_earlier_restage_still_works() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        await _stage_ai_edit(user_id, thread_id, "First hook")
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")
        healed = await _read(user_id, thread_id)  # a restage row sits under the next edit
        second_payload = copy.deepcopy(healed.snapshot["editor_payload"])
        second_payload["sections"]["text_elements"][0]["text"] = "Second hook"
        async with AsyncSessionLocal() as db:
            second = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=healed.draft_revision,
                expected_etag=healed.etag,
                snapshot={**healed.snapshot, "editor_payload": second_payload},
            )
        assert second.can_undo is True

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=second.draft_revision
            )

        sections = undone.snapshot["editor_payload"]["sections"]
        assert sections["text_elements"][0]["text"] == "First hook"
        assert sections["music_track_id"] == "song-B"
    finally:
        await async_engine.dispose()


# --- 57: a stale phone state against a sections head ------------------------------------


@pytest.mark.asyncio
async def test_stale_phone_state_against_a_sections_head_follows_the_three_way_rule() -> None:
    from app.kria.api_schemas import EditorStateIn
    from app.services.kria_editor_ops import resolve_editor_base

    user_id, thread_id, item_id, job_id = _seed(
        _variant(music_track_id="song-A", user_timeline={"slots": SLOTS_BEFORE})
    )
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(
            job_id,
            render_generation_id="generation-2",
            music_track_id="song-C",
            user_timeline={"slots": SLOTS_CUT},
        )
        cues = [{"id": "c1", "text": "Creator caption", "start_s": 0.0, "end_s": 1.0}]
        state = EditorStateIn.model_validate(
            {
                "base_generation": "generation-1",
                "client_state_id": "cs-1",
                "lanes": {
                    "music_track_id": "song-B",
                    "caption_cues": cues,
                    "timeline_slots": [
                        {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 1.0}
                    ],
                },
            }
        )
        with sync_session() as db:
            job = db.get(Job, job_id)
            head = db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
                )
            ).scalar_one()
            base = resolve_editor_base(job, job.assembly_plan["variants"][0], head, state)

        assert base.prior_payload["base_generation"] == "generation-2"
        assert base.projected["music_track_id"] == "song-C", "the dispatcher's music is newer"
        assert base.projected["caption_cues"] == cues, "a lane only the creator changed applies"
        assert "timeline_slots" not in base.prior_payload, "an older timeline is not replayed"
        assert {"music", "timeline"} <= set(base.dropped_lanes), (
            "a creator lane that could not be kept is named, not dropped silently"
        )
    finally:
        await async_engine.dispose()


# --- 58: timeline GET and draft GET agree -----------------------------------------------


@pytest.mark.asyncio
async def test_timeline_get_and_draft_get_agree_on_a_stranded_pinned_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "Pinned hook")
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0].update(
                editor_state_version=2,
                editor_timeline_mode="authored",
                editor_draft={"draft_id": staged.draft_id},
            )
            job.assembly_plan = plan
            db.commit()
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-B")
        rows_before = len(_rows(item_id))

        async with _client_for(user_id) as client:
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert timeline.status_code == 200, timeline.text
            assert len(_rows(item_id)) == rows_before, "GET stays write-free"
            served = timeline.json()["draft"]
            assert served is not None, "the draft key is present as before"

            draft = await client.get(f"/creation-threads/{thread_id}/draft")
            assert draft.status_code == 200, draft.text

        for document in (served["snapshot"], draft.json()["snapshot"]):
            sections = document["editor_payload"]["sections"]
            assert sections["music_track_id"] == "song-B"
            assert sections["text_elements"][0]["text"] == "Pinned hook"
            assert document["editor_payload"]["base_generation"] == "generation-2"
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 59: problem.recovery for a real version mismatch -----------------------------------


@pytest.mark.parametrize(
    "detail,code,recovery",
    [
        ("baseline_conflict", "baseline_conflict", "reload"),
        ("GUIDED_REVISION_STALE", "GUIDED_REVISION_STALE", "reload"),
        ("deletion_target_stale", "deletion_target_stale", "reload"),
        ("GUIDED_REVISION_TOKEN_REQUIRED", "GUIDED_REVISION_TOKEN_REQUIRED", "none"),
        ("editor_draft_unavailable", "editor_draft_unavailable", "none"),
        ("Cancelled videos cannot be edited.", "video_cancelled", "none"),
    ],
)
def test_save_refusal_recovery_is_reload_only_for_a_version_mismatch(
    detail: str, code: str, recovery: str
) -> None:
    from app.routes.plan_items import _editor_commit_problem_code, _editor_commit_problem_recovery

    assert _editor_commit_problem_code(detail) == code
    assert _editor_commit_problem_recovery(code) == recovery


# --- 60: the repair CLI reports each lane -----------------------------------------------


@pytest.mark.asyncio
async def test_repair_cli_dry_run_reports_what_apply_would_do_per_lane(capsys) -> None:
    from app.cli import repair_editor_heads as cli

    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(
            job_id,
            render_generation_id="generation-2",
            music_track_id="song-B",
        )
        before = _rows(item_id)

        capsys.readouterr()
        assert cli.main(["--item", str(item_id)]) == 0
        out = capsys.readouterr().out

        assert "lane text_elements: kept from head" in out
        assert "lane music: taken from committed" in out
        assert _rows(item_id) == before, "a dry run writes nothing"

        assert cli.main(["--item", str(item_id), "--apply"]) == 0
        capsys.readouterr()
        sections = _head(item_id)["snap"]["editor_payload"]["sections"]
        assert sections["music_track_id"] == "song-B"
        assert sections["text_elements"][0]["text"] == "AI hook"
    finally:
        await async_engine.dispose()


# --- 61: every head writer records the base ---------------------------------------------


@pytest.mark.asyncio
async def test_every_head_writer_records_the_committed_base_of_each_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(music_track_id="song-A"))
    try:
        bootstrapped = await _read(user_id, thread_id)
        assert bootstrapped.snapshot["editor_payload"]["base_lanes"]["music"]
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        written = _head(item_id)["snap"]["editor_payload"]["base_lanes"]
        assert written == bootstrapped.snapshot["editor_payload"]["base_lanes"], (
            "the written head records the committed lanes, not its own (edited) lanes"
        )
        await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        chat = _head(item_id)["snap"]["editor_payload"]
        assert chat["base_lanes"]["music"] == written["music"]
        assert "base_lanes" in _rows(item_id)[-2]["snap"]["editor_payload"], "'Your edits' parent"
    finally:
        await async_engine.dispose()
