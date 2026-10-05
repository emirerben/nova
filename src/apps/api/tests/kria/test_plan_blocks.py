"""KRI-443: live plan block feed + cancel-render (offline, no Postgres).

Contract: docs/pipelines/live-plan-blocks.md.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.kria import plan_blocks, runtime
from app.kria.runtime import RuntimeFailure, cancel_render
from app.services.creation_thread_titles import conversation_revision_matches
from app.services.job_cancel import JobCancelError
from app.tasks import kria_runtime


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_waiting_blocks_cover_all_seven_sections_in_display_order() -> None:
    blocks = plan_blocks.waiting_blocks()
    assert [b["section_id"] for b in blocks] == [
        "title",
        "clips",
        "captions",
        "music",
        "sfx",
        "overlays",
        "look",
    ]
    assert {b["state"] for b in blocks} == {"waiting"}
    assert all(b["decided_at"] is None and b["skipped"] is False for b in blocks)
    assert set(blocks[0]) == {
        "section_id",
        "state",
        "summary",
        "detail",
        "intent",
        "skipped",
        "decided_at",
    }


def test_block_rejects_unknown_section_or_state() -> None:
    with pytest.raises(ValueError):
        plan_blocks.block("post_caption", "waiting")
    with pytest.raises(ValueError):
        plan_blocks.block("title", "done")


def test_decided_block_is_stamped_and_summary_is_bounded() -> None:
    decided = plan_blocks.block("title", "decided", "x" * 500)
    assert decided["decided_at"] is not None
    assert len(decided["summary"]) <= 120


def test_blocks_from_guided_plan_uses_only_real_plan_fields() -> None:
    plan = {
        "text_elements": [{"text": "Day in Athens"}],
        "story_timeline": [{}, {}, {}],
        "resolved_duration_s": 17.6,
        "music": {"title": "Song", "artist": "Artist"},
        "editor_sound_effects": [{}, {}],
        "typography": {"style_id": "editorial_serif"},
    }
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(plan)}
    assert by_id["title"]["summary"] == "Day in Athens"
    assert by_id["clips"]["summary"] == "3 clips · 18s"
    assert by_id["music"]["summary"] == "Song · Artist"
    assert by_id["sfx"]["summary"] == "2 sound effects"
    assert by_id["look"]["summary"] == "Editorial serif"
    # Nothing in the plan -> decided + skipped, never invented.
    assert by_id["captions"]["skipped"] is True and by_id["captions"]["state"] == "decided"
    assert by_id["overlays"]["skipped"] is True
    assert all(b["state"] == "decided" for b in by_id.values())


def _prod_like_plan() -> dict:
    """Shape of the KRI-443 prod job 41978c25 plan: own song, no text, 6 clips."""
    return {
        "text_elements": [],
        "story_timeline": [{}] * 6,
        "resolved_duration_s": 25.0,
        "music": None,
        "user_song": {"mode": "background", "duration_s": 329.4},
        "typography": {"font": "Fraunces", "style_id": "guided_story_v2"},
        "narration_label_text_elements": [],
        "context_label_text_elements": [],
        "editor_sound_effects": [],
        "editor_media_overlays": [],
    }


def test_mapping_reports_the_creators_own_song_and_look_not_not_used() -> None:
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(_prod_like_plan())}
    assert by_id["music"]["summary"] == "Your song" and by_id["music"]["skipped"] is False
    assert by_id["music"]["detail"] == "Background"
    assert by_id["look"]["summary"] == "Guided story v2" and by_id["look"]["skipped"] is False
    assert by_id["clips"]["summary"] == "6 clips \u00b7 25s"
    # Truly absent lanes stay "Not used".
    for section in ("title", "captions", "sfx", "overlays"):
        assert by_id[section]["skipped"] is True


def test_mapping_counts_context_and_narration_labels_as_captions_and_voiceover() -> None:
    plan = {
        **_prod_like_plan(),
        "user_song": None,
        "narration": {"gcs_path": "x"},
        "narration_label_text_elements": [{"id": "a"}],
        "context_label_text_elements": [{"id": "b"}, {"id": "c"}],
    }
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(plan)}
    assert by_id["captions"]["summary"] == "3 captions"
    assert by_id["music"]["summary"] == "Your voiceover"
    lip = {**_prod_like_plan(), "user_song": {"mode": "lipsync"}}
    assert plan_blocks.decided_block(lip, "music")["detail"] == "Lip-sync"


def test_stage_reporter_sends_deciding_then_real_decided_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    sent: list[list[dict]] = []
    monkeypatch.setattr(plan_blocks, "emit_plan_blocks", lambda _j, blocks: sent.append(blocks))
    report = plan_blocks.make_stage_reporter("job", _prod_like_plan())
    report(("music",), "deciding")
    report(("music",), "decided")
    assert sent[0][0]["state"] == "deciding" and sent[0][0]["decided_at"] is None
    assert sent[1][0]["state"] == "decided" and sent[1][0]["summary"] == "Your song"


def test_stage_reporter_is_silent_with_the_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    boom = MagicMock(side_effect=AssertionError("must not emit"))
    monkeypatch.setattr(plan_blocks, "emit_plan_blocks", boom)
    plan_blocks.make_stage_reporter("job", _prod_like_plan())(("clips",), "deciding")
    boom.assert_not_called()


class _Stop(Exception):
    pass


def test_guided_render_paces_sections_to_real_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The feed must walk the render stages in order and never decide everything at once."""
    from app.pipeline import guided_story as gs
    from app.tasks import template_orchestrate

    plan = {
        **_prod_like_plan(),
        "compiler_version": 5,
        "transition_policy": {"type": "none", "duration_s": 0.0},
        "story_timeline": [{"duration_s": 5.0}, {"duration_s": 5.0}],
        "selected_media_ids": [],
        "mixed_media_timing": None,
        "text_elements": [],
    }
    monkeypatch.setattr(gs, "_download_selected", lambda p, t: ({}, []))
    monkeypatch.setattr(gs, "_render_moments", lambda p, loc, t: (["a.mp4", "b.mp4"], []))
    monkeypatch.setattr(gs, "_resolved_transition_boundaries", lambda p: ["cut"])
    monkeypatch.setattr(template_orchestrate, "_concat_demuxer", lambda *a, **k: None)
    monkeypatch.setattr(gs, "_audio_codec", lambda p: "aac")
    monkeypatch.setattr(gs, "_compose_guided_pretext_lanes", lambda base, *a, **k: base)
    monkeypatch.setattr(gs, "_compose_guided_sfx", lambda path, *a, **k: path)
    monkeypatch.setattr(gs.shutil, "copyfile", lambda a, b: None)
    monkeypatch.setattr(gs, "_verify_receipt", MagicMock(side_effect=_Stop))
    calls: list[tuple[tuple[str, ...], str]] = []
    with pytest.raises(_Stop):
        gs.render_execution_plan(
            plan,
            job_id="j",
            tmpdir=str(tmp_path),
            track=None,
            on_stage=lambda sections, state: calls.append((sections, state)),
        )
    assert calls == [
        (("clips",), "deciding"),
        (("clips",), "decided"),
        (("music",), "decided"),
        (("overlays",), "deciding"),
        (("overlays",), "decided"),
        (("title", "captions", "look"), "deciding"),
        (("title", "captions", "look"), "decided"),
        (("sfx",), "deciding"),
        (("sfx",), "decided"),
    ]
    # No call decides every section in one go.
    assert not any(state == "decided" and len(sections) == 7 for sections, state in calls)


def test_render_without_a_stage_callback_is_unchanged(tmp_path) -> None:
    import inspect

    from app.pipeline import guided_story as gs
    from app.tasks import generative_build as gb

    assert inspect.signature(gs.render_execution_plan).parameters["on_stage"].default is None
    # Phone path keeps the all-at-once report; the cloud render opts out.
    assert inspect.signature(gb._guided_execution_plan).parameters["emit_decided"].default is True


# ── best-effort helper ───────────────────────────────────────────────────────


def test_emit_is_a_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    boom = MagicMock(side_effect=AssertionError("must not open a session"))
    with patch.object(plan_blocks, "sync_session", boom):
        plan_blocks.emit_plan_blocks(str(uuid.uuid4()), plan_blocks.waiting_blocks())
        plan_blocks.emit_skipped_remainder(str(uuid.uuid4()))
    boom.assert_not_called()


def test_emit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    with patch.object(plan_blocks, "sync_session", side_effect=RuntimeError("db down")):
        plan_blocks.emit_plan_blocks(str(uuid.uuid4()), plan_blocks.waiting_blocks())
        plan_blocks.emit_skipped_remainder(str(uuid.uuid4()))
        plan_blocks.emit_plan_blocks("not-a-uuid", plan_blocks.waiting_blocks())


class _Res:
    def __init__(self, value=None, many=None):  # noqa: ANN001
        self._value = value
        self._many = many or []

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value

    def scalar_one(self):  # noqa: ANN201
        return self._value

    def scalars(self):  # noqa: ANN201
        return iter(self._many)


class _SyncDb:
    def __init__(self, job, results):  # noqa: ANN001
        self.job = job
        self.results = list(results)
        self.committed = 0

    def get(self, _model, _ident):  # noqa: ANN001, ANN201
        return self.job

    def execute(self, _stmt):  # noqa: ANN001, ANN201
        return self.results.pop(0)

    def commit(self) -> None:
        self.committed += 1


def _job(status: str = "processing") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        content_plan_item_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


def _patch_sessions(db):  # noqa: ANN001, ANN202
    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    return patch.object(plan_blocks, "sync_session", sessions)


def test_emit_appends_one_system_event_and_locks_only_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    job = _job("processing")
    thread = SimpleNamespace(id=uuid.uuid4(), runtime_version=2)
    turn_id = uuid.uuid4()
    db = _SyncDb(job, [_Res(thread.id), _Res(turn_id), _Res(thread)])
    appended: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        appended.append(kwargs)

    with (
        _patch_sessions(db),
        patch("app.tasks.kria_runtime._append_sync_event", append),
    ):
        plan_blocks.emit_plan_blocks(job.id, [plan_blocks.block("music", "deciding")])
    assert db.committed == 1
    assert appended[0]["role"] == "system" and appended[0]["event_type"] == "plan_block"
    assert appended[0]["payload"]["turn_id"] == str(turn_id)
    assert appended[0]["payload"]["job_id"] == str(job.id)
    assert appended[0]["payload"]["blocks"][0]["section_id"] == "music"


def test_emit_skips_a_cancelled_job_and_a_threadless_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    cancelled = _job("cancelled")
    appended: list = []
    with (
        _patch_sessions(_SyncDb(cancelled, [])),
        patch("app.tasks.kria_runtime._append_sync_event", lambda *a, **k: appended.append(k)),
    ):
        plan_blocks.emit_plan_blocks(cancelled.id, plan_blocks.waiting_blocks())
    live = _job("processing")
    with (
        _patch_sessions(_SyncDb(live, [_Res(None)])),
        patch("app.tasks.kria_runtime._append_sync_event", lambda *a, **k: appended.append(k)),
    ):
        plan_blocks.emit_plan_blocks(live.id, plan_blocks.waiting_blocks())
    assert appended == []


def test_finalize_sweep_marks_only_undecided_sections_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    job = _job("variants_ready")
    jid = str(job.id)
    events = [
        {"job_id": jid, "blocks": plan_blocks.waiting_blocks()},
        {"job_id": jid, "blocks": [plan_blocks.block("title", "decided", "Hi")]},
        {"job_id": "other", "blocks": [plan_blocks.block("clips", "decided", "x")]},
    ]
    db = _SyncDb(job, [_Res(uuid.uuid4()), _Res(many=events)])
    captured: list = []
    with (
        _patch_sessions(db),
        patch.object(plan_blocks, "emit_plan_blocks", lambda j, b: captured.append((j, b))),
    ):
        plan_blocks.emit_skipped_remainder(job.id)
    (_, blocks) = captured[0]
    assert [b["section_id"] for b in blocks] == [
        "clips",
        "captions",
        "music",
        "sfx",
        "overlays",
        "look",
    ]
    assert all(b["state"] == "decided" and b["skipped"] is True for b in blocks)


def test_finalize_sweep_is_silent_when_the_feed_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    job = _job("variants_ready")
    db = _SyncDb(job, [_Res(uuid.uuid4()), _Res(many=[])])
    captured: list = []
    with (
        _patch_sessions(db),
        patch.object(plan_blocks, "emit_plan_blocks", lambda j, b: captured.append(b)),
    ):
        plan_blocks.emit_skipped_remainder(job.id)
    assert captured == []


# ── waiting blocks ride the render_queued transaction ────────────────────────


def _finish(*, flag: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    monkeypatch.setattr(settings, "live_plan_review_enabled", flag)
    job_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(), status="awaiting_approval", render_attempts=0, last_error=None
    )
    turn = SimpleNamespace(id=uuid.uuid4(), status="executing")
    approval = SimpleNamespace(id=uuid.uuid4())
    execution = SimpleNamespace(
        id=uuid.uuid4(), status="accepted", result={}, error=None, completed_at=None
    )
    thread = SimpleNamespace(id=uuid.uuid4())
    results = [
        _Res(None),
        _Res(None),
        *(_Res(r) for r in (session, turn, approval, execution, thread)),
    ]
    order: list = []

    class Db:
        def execute(self, _stmt):  # noqa: ANN001, ANN201
            return results.pop(0)

        def commit(self) -> None:
            order.append("commit")

    @contextmanager
    def sessions():  # noqa: ANN202
        yield Db()

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        order.append(kwargs["event_type"])
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    events: list = []
    claim = SimpleNamespace(
        session_id=session.id,
        turn_id=turn.id,
        approval_id=approval.id,
        execution_id=execution.id,
        thread_id=thread.id,
        item_id=uuid.uuid4(),
        target_variant_id=None,
        target_generation_id=None,
    )
    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
    ):
        status, _ = kria_runtime._finish_approval_dispatch(
            claim, outcome="dispatched", job_id=str(job_id)
        )
    assert status == "dispatched"
    return events, order


def test_waiting_blocks_share_the_render_queued_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, order = _finish(flag=True, monkeypatch=monkeypatch)
    assert order == ["render_queued", "plan_block", "commit"]
    waiting = events[1]
    assert waiting["role"] == "system"
    assert waiting["payload"]["turn_id"] == events[0]["payload"]["turn_id"]
    assert waiting["payload"]["job_id"] == events[0]["payload"]["job_id"]
    assert [b["state"] for b in waiting["payload"]["blocks"]] == ["waiting"] * 7


def test_flag_off_emits_no_plan_block_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _events, order = _finish(flag=False, monkeypatch=monkeypatch)
    assert order == ["render_queued", "commit"]


# ── revision tolerance ───────────────────────────────────────────────────────


def _rev_db(event_types: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: event_types))
        )
    )


@pytest.mark.asyncio
async def test_plan_block_only_gap_passes_but_a_real_gap_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    thread = SimpleNamespace(id=uuid.uuid4(), revision=14, state={})
    assert await conversation_revision_matches(_rev_db(["plan_block", "plan_block"]), thread, 12)
    assert not await conversation_revision_matches(
        _rev_db(["plan_block", "user_message"]), thread, 12
    )
    # Exact match never needs the query.
    untouched = _rev_db([])
    assert await conversation_revision_matches(untouched, thread, 14)
    untouched.execute.assert_not_awaited()
    # A client AHEAD of the server is never tolerated.
    assert not await conversation_revision_matches(_rev_db([]), thread, 20)


@pytest.mark.asyncio
async def test_flag_off_keeps_the_strict_revision_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    db = _rev_db(["plan_block"])
    thread = SimpleNamespace(id=uuid.uuid4(), revision=14, state={})
    assert not await conversation_revision_matches(db, thread, 12)
    db.execute.assert_not_awaited()


# ── cancel-render ────────────────────────────────────────────────────────────


class _AsyncDb:
    def __init__(self, results, job=None):  # noqa: ANN001
        self.results = list(results)
        self.job = job
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, stmt):  # noqa: ANN001, ANN201
        return self.results.pop(0)

    async def get(self, _model, _ident):  # noqa: ANN001, ANN201
        return self.job

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


class _AR:
    def __init__(self, value=None, first=None):  # noqa: ANN001
        self._value = value
        self._first = first

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._value

    def scalar_one(self):  # noqa: ANN201
        return self._value

    def scalars(self):  # noqa: ANN201
        return SimpleNamespace(first=lambda: self._first)


def _cancel_fixture(turn_status: str = "observing", task: bool = True):  # noqa: ANN202
    creator = uuid.uuid4()
    item_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator,
        runtime_version=2,
        status="active",
        revision=5,
        state={},
        active_plan_item_id=item_id,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=thread.id,
        status=turn_status,
        cancel_requested_at=None,
        completed_at=None,
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        target_job_id=uuid.uuid4(),
        status="dispatched",
        completed_at=None,
    )
    job = SimpleNamespace(
        id=execution.target_job_id,
        user_id=creator,
        content_plan_item_id=item_id,
        celery_task_id="t" if task else None,
    )
    session = SimpleNamespace(id=execution.session_id, status="rendering")
    return creator, thread, turn, execution, job, session


@pytest.mark.asyncio
async def test_cancel_render_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    creator, thread, turn, execution, job, session = _cancel_fixture()
    db = _AsyncDb(
        [
            _AR(thread),
            _AR(turn),
            _AR(first=execution),
            _AR(session),
            _AR(turn),
            _AR(execution),
            _AR(thread),
        ],
        job=job,
    )
    appended: list = []

    async def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        appended.append(kwargs)
        thread.revision += 1

    cancel = AsyncMock(return_value=SimpleNamespace(job_id=str(job.id)))
    revoke = MagicMock(return_value=True)
    with (
        patch("app.services.job_cancel.lock_and_cancel_job", cancel),
        patch("app.services.job_cancel.revoke_and_cleanup_job", revoke),
        patch.object(runtime, "_append_event", append),
    ):
        out = await cancel_render(
            db,
            thread_id=thread.id,
            turn_id=turn.id,
            creator_id=creator,
            expected_thread_revision=5,
        )
    assert out.status == "cancelled" and out.thread_revision == 6
    assert turn.status == "cancelled" and execution.status == "cancelled"
    assert session.status == "awaiting_feedback"
    assert appended[0]["event_type"] == "render_cancelled"
    assert appended[0]["role"] == "system"
    assert appended[0]["payload"] == {"turn_id": str(turn.id), "job_id": str(job.id)}
    assert cancel.await_args.kwargs["require_celery_task"] is True
    revoke.assert_called_once()
    assert db.committed == 1


@pytest.mark.asyncio
async def test_cancel_render_is_off_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    with pytest.raises(RuntimeFailure) as raised:
        await cancel_render(
            _AsyncDb([]),
            thread_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            expected_thread_revision=0,
        )
    assert raised.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "awaiting_approval"])
async def test_cancel_render_409_when_turn_not_running(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    creator, thread, turn, _execution, _job, _session = _cancel_fixture(status)
    db = _AsyncDb([_AR(thread), _AR(turn)])
    with pytest.raises(RuntimeFailure) as raised:
        await cancel_render(
            db,
            thread_id=thread.id,
            turn_id=turn.id,
            creator_id=creator,
            expected_thread_revision=5,
        )
    assert raised.value.status_code == 409
    assert raised.value.code == "turn_not_cancellable"


@pytest.mark.asyncio
async def test_cancel_render_409_when_the_job_has_no_celery_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device render has no worker task: the shared service refuses it."""
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    creator, thread, turn, execution, job, _session = _cancel_fixture(task=False)
    db = _AsyncDb([_AR(thread), _AR(turn), _AR(first=execution)], job=job)
    refuse = AsyncMock(side_effect=JobCancelError(409, "This render has no cancellable task."))
    with patch("app.services.job_cancel.lock_and_cancel_job", refuse):
        with pytest.raises(RuntimeFailure) as raised:
            await cancel_render(
                db,
                thread_id=thread.id,
                turn_id=turn.id,
                creator_id=creator,
                expected_thread_revision=5,
            )
    assert raised.value.status_code == 409
    assert raised.value.code == "turn_not_cancellable"
    assert turn.status == "observing"  # nothing was marked


@pytest.mark.asyncio
async def test_cancel_render_409_on_a_stale_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    creator, thread, turn, *_ = _cancel_fixture()
    thread.revision = 9
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _AR(thread),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["user_message"])),
            ]
        ),
    )
    with pytest.raises(RuntimeFailure) as raised:
        await cancel_render(
            db,
            thread_id=thread.id,
            turn_id=turn.id,
            creator_id=creator,
            expected_thread_revision=5,
        )
    assert raised.value.code == "thread_revision_stale"


# ── capability ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_capabilities_expose_the_flag(flag: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routes.creation_threads import CreationCapabilitiesOut, capabilities

    monkeypatch.setattr(settings, "live_plan_review_enabled", flag)
    body = await capabilities(SimpleNamespace(id=uuid.uuid4()))
    assert body["live_plan_review_enabled"] is flag
    assert CreationCapabilitiesOut.model_validate(body).live_plan_review_enabled is flag
