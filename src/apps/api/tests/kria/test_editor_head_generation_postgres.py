"""Real-Postgres coverage for the invariant "the editor draft head is on the current generation".

Each test is a case of ``docs/reviews/latest-wins-convergence/server-failure-modes.md``.
Assertions are rows and returned documents only: how many head rows exist, which generation
they name, what the document says. Heads are built the way production builds them: an AI chat
head through a real draft turn (flat lanes AND a ``sections`` object) and a phone-written head
through ``write_draft`` with a full ``sections`` body.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import (
    bootstrap_editor_payload,
    read_or_bootstrap_draft,
    undo_draft,
    write_draft,
)
from app.kria.editor_head import (
    current_bases,
    flat_lane_names,
    is_head_current,
    restage_editor_head,
    restage_editor_head_sync,
)
from app.models import CreationThread, CreatorAgentSession, CreatorEditDraft, Job, PlanItem
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.editor_empty_drafts import attach_saved_editor_drafts
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_editor_state_turns_postgres import _plan_with, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

VARIANT = "original_text"
HOOK = {
    "id": "hook",
    "text": "Old matcha hook",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "font_family": "Playfair Display",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )


def _timeline() -> dict:
    return {
        "beat_grid": [],
        "slots": [
            {
                "slot_id": f"s{i}",
                "clip_index": i,
                "source_gcs_path": f"users/u/{i}.mp4",
                "source_duration_s": 8.0,
                "in_s": 0.0,
                "duration_beats": None,
                "duration_s": 4.0,
                "removed": False,
                "transition_after": "cut",
            }
            for i in range(2)
        ],
    }


def _variant(
    generation: str | None = "generation-1", text: str = "Old matcha hook", **extra
) -> dict:  # noqa: ANN003
    variant = {
        "variant_id": VARIANT,
        "resolved_archetype": "montage",
        "render_status": "ready",
        "render_finished_at": "2026-09-07T08:00:00Z",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "text_elements": [{**HOOK, "text": text}],
        "ai_timeline": _timeline(),
        "editor_capabilities": {"mix": True},
        "mix": 0.5,
        **extra,
    }
    if generation is not None:
        variant["render_generation_id"] = generation
    return variant


def _add_job(item_id, user_id, variant: dict, *, current: bool = True, **plan) -> uuid.UUID:  # noqa: ANN001, ANN003
    job_id = uuid.uuid4()
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item_id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
                assembly_plan={"variants": [variant], **plan},
            )
        )
        db.flush()
        if current:
            item.current_job_id = job_id
        db.commit()
    return job_id


def _seed(variant: dict | None = None):  # noqa: ANN202
    """Project, current Job and a session pointing at the variant (as an editor turn expects)."""
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    variant = variant or _variant()
    job_id = _add_job(item_id, user_id, variant)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        session.target_job_id = job_id
        session.target_variant_id = VARIANT
        session.target_generation_id = variant.get("render_generation_id")
        session.manifest_hash = "a" * 64
        db.commit()
    return user_id, thread_id, item_id, job_id


def _update_variant(job_id, **changes) -> None:  # noqa: ANN001, ANN003
    """Move the variant the way a render/dispatcher does: replace the JSON dict, never mutate."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        assembly = copy.deepcopy(job.assembly_plan)
        variant = assembly["variants"][0]
        for key, value in changes.items():
            if value is None:
                variant.pop(key, None)
            else:
                variant[key] = value
        job.assembly_plan = assembly
        db.commit()


def _publish_from(job_id, *, source: str, attempt: str) -> None:  # noqa: ANN001
    """A phone publish: the variant now carries the attempt id; the record names the source."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0]["render_generation_id"] = attempt
        assembly[DEVICE_RENDER_FIELD] = {
            VARIANT: {
                "published_attempt": attempt,
                "status": {"phase": "published", "base_generation": source},
            }
        }
        job.assembly_plan = assembly
        db.commit()


async def _phone_head(user_id, thread_id, text: str = "Phone hook"):  # noqa: ANN001, ANN202
    """A phone-written head: a full-``sections`` body PUT over the bootstrapped draft."""
    async with AsyncSessionLocal() as db:
        initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    payload = copy.deepcopy(initial.snapshot["editor_payload"])
    payload["editor_state_version"] = 2
    payload["sections"]["text_elements"][0]["text"] = text
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=initial.draft_revision,
            expected_etag=initial.etag,
            snapshot={**initial.snapshot, "editor_payload": payload, "changes": ["Phone edit"]},
        )


async def _chat_head(  # noqa: ANN202
    monkeypatch: pytest.MonkeyPatch,
    user_id,
    thread_id,
    text: str = "AI hook",  # noqa: ANN001
):
    """An AI chat head from a real draft turn over the bootstrapped head.

    The turn edits the hook text, shortens the first clip and sets the music level, so the
    payload carries flat ``text_elements`` / ``timeline_slots`` / ``mix`` lanes and a
    ``sections`` object, exactly what production stores.
    """
    async with AsyncSessionLocal() as db:
        await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    _plan_with(
        monkeypatch,
        [
            {"op": "edit_text", "bar_index": 0, "text": text},
            {"op": "set_clip_duration", "slot_index": 0, "duration_s": 3.0},
            {"op": "set_mix", "music_level": 0.2},
        ],
    )
    accepted = await _submit(user_id, thread_id, "Retitle it")
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result


def _restage(  # noqa: ANN202
    item_id,  # noqa: ANN001
    job_id,  # noqa: ANN001
    user_id,  # noqa: ANN001
    *,
    operation,  # noqa: ANN001
    reason: str = "test",
    lanes: frozenset[str] = frozenset(),
    lock_parents: bool = True,
):
    """Run it as the callers do: Item then Job locked first, head locked last by the module."""
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=lock_parents)
        job = db.get(Job, job_id, with_for_update=lock_parents)
        row = restage_editor_head_sync(
            db,
            item=item,
            job=job,
            variant_key=VARIANT,
            creator_id=user_id,
            operation=operation,
            reason=reason,
            lanes=lanes,
        )
        row_id = row.id if row is not None else None
        db.commit()
        return row_id


def _rows(item_id) -> list[dict]:  # noqa: ANN001
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.variant_key == VARIANT)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "rev": row.draft_revision,
                "parent": row.parent_draft_id,
                "head": row.is_head,
                "job": row.base_job_id,
                "col": row.base_generation_id,
                "payload_base": ((row.snapshot_json or {}).get("editor_payload") or {}).get(
                    "base_generation"
                ),
                "snap": row.snapshot_json,
            }
            for row in rows
        ]


def _head(item_id) -> dict:  # noqa: ANN001
    heads = [row for row in _rows(item_id) if row["head"]]
    assert len(heads) == 1, "exactly one head row per (item, variant)"
    return heads[0]


def _payload(row: dict) -> dict:
    return row["snap"]["editor_payload"]


def _flat(row: dict) -> dict:
    payload = _payload(row)
    return {key: payload[key] for key in flat_lane_names(payload)}


def _sections(row: dict) -> dict:
    return _payload(row)["sections"]


def _text(row: dict) -> str:
    return _sections(row)["text_elements"][0]["text"]


def _committed_sections(job_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, job_id)
        variant = job.assembly_plan["variants"][0]
        baseline = variant["render_generation_id"]
        return bootstrap_editor_payload(job, VARIANT, baseline)["sections"]


def _current(item_id, job_id) -> bool:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, job_id)
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == item_id,
                CreatorEditDraft.variant_key == VARIANT,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        return is_head_current(job, job.assembly_plan["variants"][0], head)


OPERATIONS = ["restamp", "retire", "drop_lanes", "heal"]


@pytest.mark.asyncio
async def test_retire_makes_the_head_the_committed_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        staged = _head(item_id)
        assert sorted(_flat(staged)) == ["mix", "text_elements", "timeline_slots"]
        assert "sections" in _payload(staged), "a chat head carries both shapes"
        # The approved edit rendered: the variant moves to G2 and shows the AI text.
        _update_variant(
            job_id, render_generation_id="generation-2", text_elements=[{**HOOK, "text": "AI hook"}]
        )
        assert not _current(item_id, job_id)

        new_id = _restage(item_id, job_id, user_id, operation="retire", reason="approval")

        head = _head(item_id)
        assert head["id"] == new_id and head["parent"] == staged["id"]
        assert head["rev"] == staged["rev"] + 1 and head["job"] == job_id
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _flat(head) == {}, "no flat lane survives a retire"
        assert _sections(head) == _committed_sections(job_id)
        assert _text(head) == "AI hook"
        assert head["snap"]["changes"] == []
        assert "editor_text_diff" not in head["snap"]
        assert head["snap"]["restage"] == {"of": str(staged["id"]), "reason": "approval"}
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_restamp_moves_only_the_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant("generation-2"))
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        staged = _head(item_id)
        _publish_from(job_id, source="generation-2", attempt="attempt-3")
        with sync_session() as db:
            job = db.get(Job, job_id)
            variant = job.assembly_plan["variants"][0]
            assert current_bases(job, variant) == frozenset({"attempt-3", "generation-2"})
        # The head still names the pre-publish generation; the publish record vouches for it.
        assert _head(item_id)["col"] == "generation-2" and _current(item_id, job_id)

        _restage(item_id, job_id, user_id, operation="restamp", reason="phone_publish")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "attempt-3"
        expected = copy.deepcopy(staged["snap"])
        expected["editor_payload"]["base_generation"] = "attempt-3"
        assert {k: v for k, v in head["snap"].items() if k != "restage"} == expected, (
            "same document, new id: byte-identical content"
        )
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_heal_keeps_the_flat_lanes_and_takes_sections_from_the_committed_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        staged = _head(item_id)
        # A dispatcher nobody named re-renders: new generation, other committed music.
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )
        assert not _current(item_id, job_id)

        _restage(item_id, job_id, user_id, operation="heal", reason="observe")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-legacy"
        assert _flat(head) == _flat(staged), "the AI's unsaved edit stays reachable"
        assert _sections(head) == _committed_sections(job_id)
        assert _sections(head)["music_track_id"] == "song-dispatch"
        assert _text(head) == "Old matcha hook", "committed sections, not the head's older copy"
        assert head["snap"]["changes"] == staged["snap"]["changes"]
        assert head["snap"]["editor_text_diff"] == staged["snap"]["editor_text_diff"]
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_heal_of_a_head_with_no_flat_lane_is_the_committed_document() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _phone_head(user_id, thread_id, "Phone hook")
        staged = _head(item_id)
        assert _flat(staged) == {} and _text(staged) == "Phone hook"
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )

        _restage(item_id, job_id, user_id, operation="heal", reason="observe")

        head = _head(item_id)
        assert _sections(head) == _committed_sections(job_id)
        assert _sections(head)["music_track_id"] == "song-dispatch"
        assert _text(head) == "Old matcha hook"
        assert head["snap"]["changes"] == []
        assert head["snap"]["editor_payload"]["editor_state_version"] == 2
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_drop_lanes_removes_only_the_named_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.kria.editor_head import TIMELINE_LANES

    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        staged = _head(item_id)
        cut = _timeline()
        cut["slots"] = cut["slots"][:1]
        _update_variant(job_id, render_generation_id="generation-cut", ai_timeline=cut)

        _restage(
            item_id, job_id, user_id, operation="drop_lanes", lanes=TIMELINE_LANES, reason="cut"
        )

        head = _head(item_id)
        assert sorted(_flat(head)) == ["mix", "text_elements"]
        assert _flat(head)["text_elements"] == _flat(staged)["text_elements"]
        assert [s["slot_id"] for s in _sections(head)["timeline_slots"]] == ["s0"]
        assert _sections(head) == _committed_sections(job_id)
        assert head["snap"]["changes"] == staged["snap"]["changes"]

        # Dropping every remaining lane leaves the committed document and no pending diff.
        _update_variant(job_id, render_generation_id="generation-cut-2")
        _restage(
            item_id,
            job_id,
            user_id,
            operation="drop_lanes",
            lanes=frozenset({"text_elements", "mix"}),
            reason="cut",
        )
        head = _head(item_id)
        assert _flat(head) == {} and head["snap"]["changes"] == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restamp", "retire", "drop_lanes", "heal"])
async def test_an_empty_state_variant_keeps_the_draft_only_content_under_every_operation(
    operation: str,
) -> None:
    from app.kria.editor_head import TIMELINE_LANES

    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _phone_head(user_id, thread_id, "Draft only text")
        # The content exists only in the draft; the committed variant is the last non-empty render.
        _update_variant(job_id, editor_state="empty", render_generation_id="generation-2")

        _restage(
            item_id,
            job_id,
            user_id,
            operation=operation,
            lanes=TIMELINE_LANES if operation == "drop_lanes" else frozenset(),
        )

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _text(head) == "Draft only text"
        assert (head["snap"]["changes"] == []) == (operation == "retire")
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_retried_operation_inserts_zero_additional_rows(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        _update_variant(job_id, render_generation_id="generation-2")

        first = _restage(item_id, job_id, user_id, operation=operation)
        rows_after_first = _rows(item_id)
        others = [_restage(item_id, job_id, user_id, operation=name) for name in OPERATIONS]

        assert first is not None
        assert others == [None] * len(OPERATIONS)
        assert _rows(item_id) == rows_after_first
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_head_of_an_older_job_is_retired_whatever_the_operation(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    user_id, thread_id, item_id, old_job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, "Old job AI hook")
        # A regenerate: the item now points at a new job with its own render lineage.
        new_job_id = _add_job(item_id, user_id, _variant("generation-1", text="New job hook"))
        assert _head(item_id)["job"] == old_job_id
        # Same generation string on purpose: only the job differs.
        assert not _current(item_id, new_job_id)

        _restage(item_id, new_job_id, user_id, operation=operation)

        head = _head(item_id)
        assert head["job"] == new_job_id
        assert head["col"] == head["payload_base"] == "generation-1"
        assert _flat(head) == {} and _text(head) == "New job hook"
        assert _current(item_id, new_job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_pinned_editor_draft_on_a_v2_variant_follows_the_restaged_head() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _phone_head(user_id, thread_id, "Phone hook")
        _update_variant(
            job_id,
            editor_state_version=2,
            editor_draft={"draft_id": staged.draft_id},
        )

        async def pinned_draft_ids() -> dict:
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                await attach_saved_editor_drafts(db, job)
                return {k: v.draft_id for k, v in job._saved_editor_drafts.items()}

        assert await pinned_draft_ids() == {VARIANT: staged.draft_id}
        _update_variant(job_id, render_generation_id="generation-2")
        assert await pinned_draft_ids() == {}, "the strict read never serves a stale pinned draft"

        new_id = _restage(item_id, job_id, user_id, operation="retire", reason="approval")

        with sync_session() as db:
            variant = db.get(Job, job_id).assembly_plan["variants"][0]
            assert variant["editor_draft"] == {"draft_id": str(new_id)}
            assert variant["editor_state_version"] == 2
        assert await pinned_draft_ids() == {VARIANT: str(new_id)}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_reading_a_stranded_chat_head_heals_it_and_keeps_the_ai_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        staged = _head(item_id)
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )
        assert not _current(item_id, job_id)

        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert served.head_current is True
        assert served.base_generation == served.base_generation_id == "generation-legacy"
        payload = served.snapshot["editor_payload"]
        assert payload["base_generation"] == "generation-legacy"
        assert {k: payload[k] for k in flat_lane_names(payload)} == _flat(staged)
        assert payload["sections"] == _committed_sections(job_id)
        assert served.snapshot["changes"] == staged["snap"]["changes"]
        assert _current(item_id, job_id)

        rows_after_heal = _rows(item_id)
        async with AsyncSessionLocal() as db:
            again = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert again.draft_id == served.draft_id
        assert _rows(item_id) == rows_after_heal, "a healed head is read, not restaged again"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_reading_a_stranded_sections_only_head_serves_the_committed_document() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _phone_head(user_id, thread_id, "Phone hook")
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )

        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert served.head_current is True
        payload = served.snapshot["editor_payload"]
        assert flat_lane_names(payload) == []
        assert payload["sections"] == _committed_sections(job_id)
        assert served.snapshot["changes"] == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_reading_a_head_published_from_its_generation_is_moved_to_the_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant("generation-2"))
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        _publish_from(job_id, source="generation-2", attempt="attempt-3")

        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert served.base_generation == "attempt-3" and served.head_current is True
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "attempt-3"
        assert _flat(head)["text_elements"][0]["text"] == "AI hook"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_put_naming_a_head_that_had_to_be_healed_is_draft_stale_and_writes_only_the_heal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.runtime import RuntimeFailure

    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        async with AsyncSessionLocal() as db:
            seen = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        # The generation moves after the client read its copy.
        _update_variant(job_id, render_generation_id="generation-2", music_track_id="song-x")
        committed = copy.deepcopy(_committed_sections(job_id))
        rows_before = _rows(item_id)

        body = copy.deepcopy(seen.snapshot)
        body["editor_payload"]["sections"]["text_elements"][0]["text"] = "Phone edit on G1"
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as raised:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=seen.draft_revision,
                    expected_etag=seen.etag,
                    snapshot=body,
                )
        assert raised.value.code == "draft_stale" and raised.value.recovery == "refresh_replan"

        rows = _rows(item_id)
        assert len(rows) == len(rows_before) + 1, "only the heal was written"
        head = _head(item_id)
        assert head["snap"]["restage"]["reason"] == "observe"
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _sections(head) == committed, "the committed content is intact"
        assert "Phone edit on G1" not in str(head["snap"])
        assert _flat(head)["text_elements"][0]["text"] == "AI hook"
        assert _current(item_id, job_id)

        # The phone re-reads and converges locally; its next write applies.
        async with AsyncSessionLocal() as db:
            reread = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert reread.draft_id == str(head["id"])
        body = copy.deepcopy(reread.snapshot)
        body["editor_payload"]["sections"]["text_elements"][0]["text"] = "Converged"
        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=reread.draft_revision,
                expected_etag=reread.etag,
                snapshot=body,
            )
        assert written.head_current is True and written.base_generation == "generation-2"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_put_body_naming_an_old_generation_is_draft_stale_not_restamped() -> None:
    from app.kria.runtime import RuntimeFailure

    user_id, thread_id, item_id, job_id = _seed()
    try:
        phone = await _phone_head(user_id, thread_id, "Phone hook")
        rows_before = _rows(item_id)
        # The head is current and the If-Match matches, but the body was authored on G0.
        body = copy.deepcopy(phone.snapshot)
        body["editor_payload"]["base_generation"] = "generation-0"
        body["editor_payload"]["sections"]["text_elements"][0]["text"] = "Authored on G0"
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as raised:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=phone.draft_revision,
                    expected_etag=phone.etag,
                    snapshot=body,
                )
        assert raised.value.code == "draft_stale"
        assert _rows(item_id) == rows_before
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_write_with_a_stale_revision_is_still_refused_as_draft_stale() -> None:
    from app.kria.runtime import RuntimeFailure

    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _phone_head(user_id, thread_id, "Phone hook")
        rows_before = _rows(item_id)

        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as raised:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=staged.draft_revision - 1,
                    expected_etag=staged.etag,
                    snapshot=staged.snapshot,
                )
        assert raised.value.code == "draft_stale"
        assert _rows(item_id) == rows_before
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_after_a_restage_skips_it_and_restores_the_earlier_draft_on_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        _update_variant(job_id, render_generation_id="generation-2")
        async with AsyncSessionLocal() as db:
            healed = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        restaged = _head(item_id)
        assert restaged["snap"]["restage"]["reason"] == "observe"
        assert healed.can_undo is True, "the AI edit can still be undone after the restage"

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=restaged["rev"]
            )

        head = _head(item_id)
        assert head["id"] == uuid.UUID(undone.draft_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _current(item_id, job_id)
        assert undone.head_current is True and undone.base_generation == "generation-2"
        assert _flat(head) == {} and _text(head) == "Old matcha hook", (
            "the parent of the AI edit, not the restage row, moved onto the current generation"
        )
        assert "restage" not in head["snap"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_of_a_head_on_an_old_generation_still_lands_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, "AI hook")
        await _chat_head(monkeypatch, user_id, thread_id, "Second hook")
        second = _head(item_id)
        _update_variant(job_id, render_generation_id="generation-2")

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=second["rev"]
            )

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _current(item_id, job_id)
        assert undone.head_current is True
        assert _flat(head)["text_elements"][0]["text"] == "AI hook"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_can_undo_is_computed_one_way_by_the_draft_and_the_timeline_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        head = _head(item_id)
        _update_variant(
            job_id,
            render_generation_id="generation-2",
            editor_state_version=2,
            editor_draft={"draft_id": str(head["id"])},
        )
        _restage(item_id, job_id, user_id, operation="heal", reason="observe")

        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            await attach_saved_editor_drafts(db, job, rebase_stale=True)
            timeline_draft = job._saved_editor_drafts[VARIANT]
        async with AsyncSessionLocal() as db:
            read = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert timeline_draft.can_undo is read.can_undo is True
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_is_current_only_when_column_and_payload_bases_both_are() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _phone_head(user_id, thread_id)
        assert _current(item_id, job_id)

        def set_bases(column: str, payload: str) -> None:
            with sync_session() as db:
                row = db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
                    )
                ).scalar_one()
                snapshot = copy.deepcopy(row.snapshot_json)
                snapshot["editor_payload"]["base_generation"] = payload
                row.snapshot_json = snapshot
                row.base_generation_id = column
                db.commit()

        set_bases("generation-1", "generation-0")
        assert not _current(item_id, job_id)
        set_bases("generation-0", "generation-1")
        assert not _current(item_id, job_id)
        set_bases("generation-1", "generation-1")
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_variant_with_only_render_finished_at_is_stamped_and_healed_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(None))
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "2026-09-07T08:00:00Z"
        assert _current(item_id, job_id)

        _update_variant(job_id, render_finished_at="2026-09-07T09:00:00Z")
        assert not _current(item_id, job_id)
        _restage(item_id, job_id, user_id, operation="heal", reason="baseline")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "2026-09-07T09:00:00Z"
        assert _flat(head)["text_elements"][0]["text"] == "AI hook"
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_variant_that_never_finished_a_render_has_a_current_head_and_no_heal_rows() -> None:
    variant = _variant(None)
    variant.pop("render_finished_at")
    user_id, thread_id, item_id, job_id = _seed(variant)
    try:
        async with AsyncSessionLocal() as db:
            first = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        async with AsyncSessionLocal() as db:
            again = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert first.draft_id == again.draft_id
        assert again.head_current is True and again.base_generation == ""
        assert _current(item_id, job_id)
        assert len(_rows(item_id)) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_non_editor_head_and_a_missing_head_are_left_alone() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    try:
        with sync_session() as db:
            item_id = db.get(CreationThread, thread_id).active_plan_item_id
        # No job yet: the bootstrap draft is an "initial" body with no editor payload.
        async with AsyncSessionLocal() as db:
            initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert initial.snapshot["kind"] == "initial"
        job_id = _add_job(item_id, user_id, _variant("generation-2"))
        before = _rows(item_id)

        assert _restage(item_id, job_id, user_id, operation="retire") is None
        assert _rows(item_id) == before

        other_user, _other_thread, other_item, other_job = _seed()
        assert _restage(other_item, other_job, other_user, operation="retire") is None
        assert _rows(other_item) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_operation_waits_for_a_concurrent_writer_and_moves_the_writers_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        _update_variant(job_id, render_generation_id="generation-2")
        holding, release = threading.Event(), threading.Event()

        def writer() -> None:
            with sync_session() as db:
                head = db.execute(
                    select(CreatorEditDraft)
                    .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
                    .with_for_update()
                ).scalar_one()
                holding.set()
                assert release.wait(20)
                snapshot = copy.deepcopy(head.snapshot_json)
                snapshot["editor_payload"]["text_elements"][0]["text"] = "Writer text"
                head.is_head = False
                db.add(
                    CreatorEditDraft(
                        creator_id=head.creator_id,
                        thread_id=head.thread_id,
                        item_id=head.item_id,
                        variant_key=head.variant_key,
                        base_job_id=head.base_job_id,
                        base_generation_id=head.base_generation_id,
                        draft_revision=head.draft_revision + 1,
                        parent_draft_id=head.id,
                        snapshot_json=snapshot,
                        snapshot_hash="writer-" + uuid.uuid4().hex,
                        is_head=True,
                    )
                )
                db.commit()

        writer_task = asyncio.create_task(asyncio.to_thread(writer))
        await asyncio.to_thread(holding.wait, 20)
        restage_task = asyncio.create_task(
            asyncio.to_thread(
                _restage,
                item_id,
                job_id,
                user_id,
                operation="heal",
                reason="race",
                # The writer below locks only the head, so it would deadlock against a held
                # Item/Job lock through its foreign keys. Real writers take Item first.
                lock_parents=False,
            )
        )
        await asyncio.sleep(0.6)
        assert not restage_task.done(), "the restage must wait on the head lock the writer holds"
        release.set()
        await writer_task
        assert await restage_task is not None

        rows = _rows(item_id)
        head = _head(item_id)
        assert head["rev"] == rows[-1]["rev"] and head["parent"] == rows[-2]["id"]
        assert rows[-2]["snap"]["editor_payload"]["text_elements"][0]["text"] == "Writer text"
        assert _flat(head)["text_elements"][0]["text"] == "Writer text", (
            "the revision that won the lock is what gets moved"
        )
        assert head["col"] == head["payload_base"] == "generation-2"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_async_twin_restages_exactly_like_the_sync_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id)
        _update_variant(job_id, render_generation_id="generation-2")

        async def run() -> uuid.UUID | None:
            async with AsyncSessionLocal() as db:
                item = (
                    await db.execute(
                        select(PlanItem).where(PlanItem.id == item_id).with_for_update()
                    )
                ).scalar_one()
                job = (
                    await db.execute(select(Job).where(Job.id == job_id).with_for_update())
                ).scalar_one()
                row = await restage_editor_head(
                    db,
                    item=item,
                    job=job,
                    variant_key=VARIANT,
                    creator_id=user_id,
                    operation="retire",
                    reason="approval",
                )
                row_id = row.id if row is not None else None
                await db.commit()
                return row_id

        first = await run()
        head = _head(item_id)
        assert first == head["id"]
        assert head["col"] == head["payload_base"] == "generation-2"
        assert await run() is None
        assert len(_rows(item_id)) == 3  # bootstrap, chat turn, the retire
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_locking_the_job_rereads_a_generation_committed_after_the_target_was_loaded() -> None:
    from app.kria.drafts import _lock_job, _target

    user_id, thread_id, item_id, job_id = _seed()
    try:
        async with AsyncSessionLocal() as db:
            target = await _target(db, thread_id=thread_id, creator_id=user_id, lock_item=True)
            assert target.generation_id == "generation-1"
            _update_variant(job_id, render_generation_id="generation-2")
            relocked = await _lock_job(db, target)
            assert relocked.generation_id == "generation-2"
    finally:
        await async_engine.dispose()


def test_current_bases_drops_empties_and_falls_back_to_render_finished_at() -> None:
    job = SimpleNamespace(assembly_plan={})
    assert current_bases(job, {"variant_id": VARIANT}) == frozenset()
    assert current_bases(job, {"variant_id": VARIANT, "render_finished_at": "t1"}) == frozenset(
        {"t1"}
    )
    assert current_bases(
        job, {"variant_id": VARIANT, "render_generation_id": "g1", "render_finished_at": "t1"}
    ) == frozenset({"g1"})
