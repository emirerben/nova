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


def test_waiting_blocks_cover_all_eight_sections_in_display_order() -> None:
    blocks = plan_blocks.waiting_blocks()
    assert [b["section_id"] for b in blocks] == [
        "title",
        "clips",
        "captions",
        "music",
        "sfx",
        "overlays",
        "look",
        "post_caption",
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
        plan_blocks.block("bogus", "waiting")
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


def _phone_recipe(*, clips=3, sfx=0, overlays=0, cutaway=0, duration_hint=None):
    def clip(i, asset):
        return SimpleNamespace(
            source_asset_id=asset,
            timeline_start=float(i * 4),
            source_duration=4.0,
            rate=1.0,
            hold_duration=None,
        )

    tracks = [
        SimpleNamespace(
            id="narrated", kind="video", clips=[clip(i, f"a{i}") for i in range(clips)]
        ),
        SimpleNamespace(id="narration", kind="audio", clips=[clip(0, "voice")]),
    ]
    if sfx:
        tracks.append(
            SimpleNamespace(id="sfx", kind="audio", clips=[clip(i, "s") for i in range(sfx)])
        )
    if overlays:
        tracks.append(
            SimpleNamespace(
                id="subtitled-overlays",
                kind="overlay",
                clips=[clip(i, "o") for i in range(overlays)],
            )
        )
    if cutaway:
        tracks.append(
            SimpleNamespace(
                id="talking-head-cutaways",
                kind="overlay",
                clips=[clip(i, "c") for i in range(cutaway)],
            )
        )
    duration = max(c.timeline_start + c.source_duration for t in tracks for c in t.clips)
    return SimpleNamespace(tracks=tracks, duration=duration)


def test_blocks_from_phone_recipe_reports_seven_decided_sections_from_the_recipe() -> None:
    recipe = _phone_recipe(clips=3, sfx=2, overlays=1, cutaway=2)
    blocks = plan_blocks.blocks_from_phone_recipe(
        recipe, title="Cacio e pepe", captions=5, music="Your voiceover", look="editorial_clean"
    )
    assert [b["section_id"] for b in blocks] == list(plan_blocks.SECTION_ORDER)
    assert {b["state"] for b in blocks} == {"decided"}
    assert all(b["decided_at"] for b in blocks)
    by_id = {b["section_id"]: b for b in blocks}
    assert by_id["title"]["summary"] == "Cacio e pepe"
    assert by_id["clips"]["summary"] == "3 clips \u00b7 12s"
    assert by_id["captions"]["summary"] == "5 captions"
    assert by_id["music"]["summary"] == "Your voiceover"
    assert by_id["sfx"]["summary"] == "2 sound effects"
    # Talking cutaways count as clips, not overlays.
    assert by_id["overlays"]["summary"] == "1 overlay"
    assert by_id["look"]["summary"] == "Editorial clean"
    # post_caption is resolved later by `emit_post_caption`; flag off it is simply unused.
    assert not any(b["skipped"] for b in blocks if b["section_id"] != "post_caption")


def test_blocks_from_phone_recipe_marks_unused_sections_not_used() -> None:
    blocks = plan_blocks.blocks_from_phone_recipe(_phone_recipe(clips=1))
    by_id = {b["section_id"]: b for b in blocks}
    assert len(blocks) == 8 and {b["state"] for b in blocks} == {"decided"}
    assert by_id["clips"]["summary"] == "1 clip \u00b7 4s" and not by_id["clips"]["skipped"]
    for section in ("title", "captions", "music", "sfx", "overlays", "look", "post_caption"):
        assert by_id[section]["skipped"] is True and by_id[section]["summary"] == "Not used"


def test_blocks_from_phone_recipe_clip_count_override_and_music_detail() -> None:
    blocks = plan_blocks.blocks_from_phone_recipe(
        _phone_recipe(clips=5),
        clip_count=2,
        music="Some Track \u00b7 Artist",
        music_detail="Under your voiceover",
    )
    by_id = {b["section_id"]: b for b in blocks}
    assert by_id["clips"]["summary"].startswith("2 clips")
    assert by_id["music"]["detail"] == "Under your voiceover"


def test_emit_phone_recipe_blocks_never_raises_and_is_silent_with_the_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = MagicMock()
    monkeypatch.setattr(plan_blocks, "emit_plan_blocks", sent)
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    plan_blocks.emit_phone_recipe_blocks("job", _phone_recipe())
    sent.assert_not_called()

    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    plan_blocks.emit_phone_recipe_blocks("job", object(), captions=2)  # no tracks -> still 7 blocks
    assert len(sent.call_args.args[1]) == 8
    sent.side_effect = RuntimeError("feed down")
    plan_blocks.emit_phone_recipe_blocks("job", _phone_recipe())
    plan_blocks.emit_phone_recipe_blocks("job", _phone_recipe(), captions="not-a-number")


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
    db = _SyncDb(job, [_Res(thread.id), _Res(turn_id), _Res(thread), _Res(many=[])])
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
        patch.object(plan_blocks, "_enrich_from_variant", lambda j: None),
        patch.object(plan_blocks, "emit_post_caption", lambda j: None),
        patch.object(plan_blocks, "_emit_update_summary", lambda j: None),
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
        "post_caption",
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
        patch.object(plan_blocks, "_enrich_from_variant", lambda j: None),
        patch.object(plan_blocks, "emit_post_caption", lambda j: None),
        patch.object(plan_blocks, "_emit_update_summary", lambda j: None),
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
        _Res(many=[]),  # dispatch_payload: the thread's earlier plan_block events
    ]
    order: list = []

    class Db:
        def execute(self, _stmt):  # noqa: ANN001, ANN201
            return results.pop(0)

        def commit(self) -> None:
            order.append("commit")

        @contextmanager
        def begin_nested(self):  # noqa: ANN201
            yield

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
    assert [b["state"] for b in waiting["payload"]["blocks"]] == ["waiting"] * 8


def test_a_broken_scoped_payload_never_fails_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Celery task is already queued: the feed falls back to plain `waiting` blocks and
    the dispatch still commits."""

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("feed problem")

    monkeypatch.setattr(plan_blocks, "dispatch_payload", boom)
    events, order = _finish(flag=True, monkeypatch=monkeypatch)
    assert order == ["render_queued", "plan_block", "commit"]
    assert [b["state"] for b in events[1]["payload"]["blocks"]] == ["waiting"] * 8


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
    # Contract v2 (payloads, GET /plan) is advertised only with the flag on; 1 = feed only.
    assert body["live_plan_review_version"] == (2 if flag else 1)


# ── contract v2: structured payloads, transitions, revision (KRI-439/447/448) ─


def _moment(i: int, *, kind: str = "video", transition=None, duration: float = 2.0) -> dict:
    return {
        "moment_id": f"m{i}",
        "media_id": f"media-{i}",
        "topic": "Opening shot of the harbour at sunrise, wide" if i == 0 else f"Beat {i}",
        "kind": kind,
        "gcs_path": f"u/{i}.mp4",
        "source_start_s": 1.0,
        "source_end_s": 1.0 + duration,
        "output_start_s": i * duration,
        "output_end_s": (i + 1) * duration,
        "duration_s": duration,
        "transition_after": transition,
        "look_preset": "golden_hour" if i == 0 else "none",
    }


def _guided_plan(clips: int = 3) -> dict:
    return {
        "story_timeline": [
            _moment(0, transition="flash"),
            _moment(1, transition="dip_to_black", kind="image"),
            *[_moment(i) for i in range(2, clips)],
        ],
        "resolved_duration_s": 2.0 * clips,
        "transition_policy": {"type": "crossfade", "duration_s": 0.4},
        "text_elements": [
            {"id": "guided-title-1", "text": "Day in Athens", "start_s": 0, "end_s": 3},
            {"id": "clip-label-m1", "text": "Plaka", "start_s": 2.0, "end_s": 4.0},
        ],
        "narration_label_text_elements": [
            {"id": "nl-1", "text": "Morning light", "start_s": 0.5, "end_s": 1.5}
        ],
        "context_label_text_elements": [],
        "music": {"track_id": "t1", "title": "Song", "level": 0.6, "start_s": 12.0},
        "editor_original_level": 0.3,
        "editor_sound_effects": [{"id": "s1", "label": "Whoosh", "at_s": 2.0, "gain": 1.0}],
        "editor_media_overlays": [
            {"id": "o1", "kind": "image", "start_s": 1, "end_s": 2, "display_mode": "pip"}
        ],
        "typography": {"style_id": "guided_story_v2", "font": "Fraunces"},
    }


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    monkeypatch.setattr(
        "app.kria.plan_payloads.lookup_track_meta",
        lambda tid: {"title": "Song", "artist": "Artist", "bpm": 120.0},
    )


def test_guided_payloads_carry_transitions_in_the_shared_vocabulary(flag_on) -> None:
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(_guided_plan())}
    clips = by_id["clips"]["payload"]["clips"]
    # `transition` LEAVES the clip: per-moment value wins, the policy fills the rest, last is null.
    assert [c.get("transition") for c in clips] == ["whip", "fade", None]
    assert clips[0]["role"] == "Opening shot of the harbour at sunrise, w"[:40]
    assert clips[1]["kind"] == "image" and clips[1]["label"] == "Plaka"
    assert (clips[0]["start_s"], clips[0]["end_s"]) == (0.0, 2.0)
    assert by_id["clips"]["summary"] == "3 clips · 6s"  # old clients unchanged
    policy_plan = _guided_plan()
    policy_plan["story_timeline"][0]["transition_after"] = None
    first = plan_blocks.decided_block(policy_plan, "clips")["payload"]["clips"][0]
    assert first["transition"] == "dissolve" and first["transition_duration_s"] == 0.4


def test_unmapped_transition_is_null_never_a_guess(flag_on) -> None:
    plan = _guided_plan()
    plan["story_timeline"][0]["transition_after"] = "wipe_left"
    assert plan_blocks.decided_block(plan, "clips")["payload"]["clips"][0].get("transition") is None


def test_guided_payloads_for_every_section(flag_on) -> None:
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(_guided_plan())}
    assert by_id["title"]["payload"] == {"text": "Day in Athens", "bar_id": "guided-title-1"}
    caption_ids = [line["id"] for line in by_id["captions"]["payload"]["lines"]]
    assert caption_ids == ["nl-1"]
    music = by_id["music"]["payload"]
    assert (music["source"], music["artist"], music["bpm"], music["start_s"]) == (
        "catalog",
        "Artist",
        120.0,
        12.0,
    )
    assert music["mix"] == {"music_level": 0.6, "original_level": 0.3}
    assert by_id["sfx"]["payload"]["items"][0]["at_s"] == 2.0
    assert by_id["overlays"]["payload"]["items"][0]["kind"] == "image"
    chips = by_id["look"]["payload"]["chips"]
    assert "Guided story v2" in chips and "Fraunces titles" in chips and "Golden hour" in chips
    # post_caption: no copy on the plan yet -> waits for emit_post_caption.
    assert by_id["post_caption"]["state"] == "deciding"
    with_copy = {**_guided_plan(), "post_caption": {"text": "Athens!", "hashtags": ["#a", "b"]}}
    block = plan_blocks.blocks_from_guided_plan(with_copy)[-1]
    assert block["payload"] == {"text": "Athens!", "hashtags": ["a", "b"], "platform": "tiktok"}


def test_flag_off_blocks_are_byte_identical_to_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    blocks = plan_blocks.blocks_from_guided_plan(_guided_plan())
    assert all("payload" not in b and "revision" not in b for b in blocks)
    assert all(
        set(b) == {"section_id", "state", "summary", "detail", "intent", "skipped", "decided_at"}
        for b in blocks
    )
    assert plan_blocks.plan_block_payload(turn_id=None, job_id="j", blocks=[]) == {
        "turn_id": None,
        "job_id": "j",
        "blocks": [],
    }


def test_payload_caps_truncate_lists_and_never_raise(flag_on) -> None:
    plan = _guided_plan(clips=45)
    plan["narration_label_text_elements"] = [
        {"id": f"n{i}", "text": "x" * 190, "start_s": i, "end_s": i + 1} for i in range(120)
    ]
    plan["editor_sound_effects"] = [{"id": f"s{i}", "at_s": i} for i in range(80)]
    by_id = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(plan)}
    import json

    assert len(by_id["clips"]["payload"]["clips"]) == 40
    assert by_id["clips"]["summary"].startswith("45 clips")
    captions = by_id["captions"]["payload"]
    assert len(captions["lines"]) <= 40 and captions["truncated"] is True
    assert captions["count"] == 120  # the real total survives the truncation
    assert by_id["sfx"]["payload"]["count"] == 80 and len(by_id["sfx"]["payload"]["items"]) == 30
    for entry in by_id.values():
        assert len(json.dumps(entry.get("payload") or {}).encode()) <= 16 * 1024
    # Garbage in: no payload, the summary block survives.
    broken = {**_guided_plan(), "story_timeline": [{"output_start_s": "x"}]}
    assert plan_blocks.decided_block(broken, "clips")["state"] == "decided"


def _ev(job: str, *blocks: dict, **extra) -> dict:
    return {"job_id": job, "blocks": list(blocks), **extra}


def test_revision_changed_previous_across_two_jobs() -> None:
    b = plan_blocks.block
    first = plan_blocks.annotate_blocks(
        [b("title", "decided", "Hi", payload={"text": "Hi"}), b("clips", "waiting")], [], "j1"
    )
    assert (first[0]["revision"], first[0]["changed"], "previous" in first[0]) == (1, False, False)
    assert first[1]["revision"] == 0
    history = [_ev("j1", *first)]
    # A re-render that decides the SAME value keeps the revision.
    same = plan_blocks.annotate_blocks(
        [b("title", "decided", "Hi", payload={"text": "Hi"})], history, "j2"
    )
    assert (same[0]["revision"], same[0]["changed"]) == (1, False)
    # A different value bumps it and remembers what it replaced.
    new = plan_blocks.annotate_blocks(
        [b("title", "decided", "Bye", payload={"text": "Bye"})], history, "j2"
    )[0]
    assert (new["revision"], new["changed"]) == (2, True)
    assert new["previous"] == {
        "revision": 1,
        "job_id": "j1",
        "summary": "Hi",
        "payload": {"text": "Hi"},
        "skipped": False,
    }
    # waiting / deciding carry the revision of the value they will replace.
    deciding = plan_blocks.annotate_blocks([b("title", "deciding")], history, "j2")[0]
    assert deciding["revision"] == 1 and deciding["changed"] is False
    # Without payloads the comparison falls back to summary + skipped.
    skipped = plan_blocks.annotate_blocks(
        [b("title", "decided", "Not used", skipped=True)], history, "j2"
    )[0]
    assert skipped["changed"] is True and skipped["previous"]["skipped"] is False
    # A fill-in within the same job never lowers an already-bumped revision.
    history.append(_ev("j2", new))
    again = plan_blocks.annotate_blocks(
        [b("title", "decided", "Hi", payload={"text": "Hi"})], history, "j2"
    )[0]
    assert again["revision"] == 2 and again["changed"] is True


def test_legacy_decided_blocks_count_as_revision_one() -> None:
    legacy = _ev("j1", plan_blocks.block("music", "decided", "Song"))
    out = plan_blocks.annotate_blocks(
        [plan_blocks.block("music", "decided", "Other")], [legacy], "j2"
    )[0]
    assert out["revision"] == 2 and out["previous"]["revision"] == 1


def test_server_reducer_is_forward_only_and_higher_revision_wins() -> None:
    b = plan_blocks.block
    old = {**b("title", "decided", "Hi", payload={"text": "Hi"}), "revision": 1}
    newer = {**b("title", "decided", "Bye", payload={"text": "Bye"}), "revision": 2}
    reduced = plan_blocks.reduce_job_blocks(
        [
            _ev("j", newer),
            _ev("j", {**b("title", "deciding"), "revision": 2}),  # forward-only: ignored
            _ev("j", old),  # lower revision: ignored
            _ev("other", {**b("title", "decided", "Z"), "revision": 9}),
        ],
        "j",
    )
    assert reduced["title"]["summary"] == "Bye"
    fill = plan_blocks.reduce_job_blocks(
        [
            _ev("j", {**b("clips", "decided", "3 clips", payload={"clips": []}), "revision": 1}),
            _ev("j", {**b("clips", "decided"), "revision": 1}),
        ],
        "j",
    )
    assert fill["clips"]["summary"] == "3 clips" and fill["clips"]["payload"] == {"clips": []}


def test_update_summary_text_is_deterministic_and_localized() -> None:
    assert plan_blocks.update_summary_text(["music", "captions"]) == "Updated captions and music."
    assert plan_blocks.update_summary_text(["title", "look", "sfx"]).startswith(
        "Updated title, sound effects and look"
    )
    from app.kria.reply_language import reply_language_for

    with reply_language_for("tr"):
        assert "güncelledim" in plan_blocks.update_summary_text(["music"])


def test_cloud_decision_blocks_keep_summaries_and_add_payloads(flag_on) -> None:
    track = SimpleNamespace(id="t1", title="Song", artist="Artist")
    title, look, music = plan_blocks.cloud_decision_blocks("Hello", "editorial_serif", track)
    assert (title["summary"], look["summary"], music["summary"]) == (
        "Hello",
        "Editorial serif",
        "Song · Artist",
    )
    assert title["payload"] == {"text": "Hello"}
    assert music["payload"]["bpm"] == 120.0 and music["payload"]["source"] == "catalog"
    empty = plan_blocks.cloud_decision_blocks(None, None, None)
    assert all(b["skipped"] and "payload" not in b for b in empty)


def test_variant_payloads_for_the_cloud_path(flag_on) -> None:
    from app.kria import plan_payloads

    variant = {
        "duration_s": 9.0,
        "ai_timeline": {
            "slots": [
                {"order": 1, "duration_s": 4.0, "in_s": 2.0, "slot_type": "cutaway"},
                {"order": 0, "duration_s": 5.0, "in_s": 0.0, "slot_type": "hero"},
            ]
        },
        "caption_cues": [{"text": "hello", "start_s": 0.2, "end_s": 1.0}],
        "text_elements": [{"id": "t1", "text": "Intro", "start_s": 0, "end_s": 2}],
        "sound_effects": [{"id": "s1", "at_s": 3.0}],
        "style_set_id": "editorial_serif",
    }
    raws = plan_payloads.finalized(plan_payloads.variant_raws(variant))
    assert [(c["start_s"], c["end_s"], c["role"]) for c in raws["clips"]["clips"]] == [
        (0.0, 5.0, "hero"),
        (5.0, 9.0, "cutaway"),
    ]
    assert raws["captions"]["lines"][0]["id"] == "cue-0"  # cues without an id get cue-<index>
    assert raws["title"]["text"] == "Intro" and raws["sfx"]["count"] == 1
    assert raws["music"] is None and raws["overlays"] is None
