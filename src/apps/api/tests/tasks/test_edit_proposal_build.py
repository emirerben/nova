from __future__ import annotations

import json
import uuid
from contextlib import contextmanager, nullcontext
from threading import Event, Lock
from time import sleep
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import Retry

import app.tasks.edit_proposal_build as proposal_build
from app.schemas.edit_proposal import (
    EditProposal,
    FastMontageCut,
    MediaRef,
    ProposalBrief,
    parse_edit_proposal,
)
from app.tasks.autoplace import (
    ANALYSIS_VERSION,
    AnalysisTemporarilyUnavailableError,
    AssetUnreadableError,
)

# Exact prod clip_assignments payload (plan item 85d1de16-ba11-4533-9290-927a45819cd3):
# one 6.77s landscape .mov, zero pool assets, edit_proposal.status="failed" with
# failure.code="proposal_generation_failed" and zero recorded agent_run rows.
_PROD_CLIP_ASSIGNMENT = json.loads(
    """
{"kind": "video", "aspect": 1.7778, "shot_id": null, "analysis": {"width": 1920, "brands": [],
"height": 1080, "source": "clip_metadata", "subject": "Acropolis of Athens",
"kind_hint": "screenshot", "duration_s": 6.768333, "description": "",
"best_moments": [{"end_s": 0.0, "energy": 0.0, "start_s": 0.0, "description": ""},
{"end_s": 0.0, "energy": 0.0, "start_s": 0.0, "description": ""}], "on_screen_text": "",
"analysis_version": 5}, "gcs_path":
"users/25a596e6-0ada-4572-b669-f4f9d5c5aced/plan/85d1de16-ba11-4533-9290-927a45819cd3/2d3cc760377d4b6993467a2955d4c945-IMG_5319.mov",
"media_id": "85fcc2f9-12c2-42e8-9fd6-6b2d767075cb", "user_note": "", "duration_s": 6.768333,
"generation": "1787000010652201", "machine_matched": false}
"""
)
# The literal payload above is frozen at the analysis_version that was live
# when it was captured (5). Every OTHER test in this file uses it purely as a
# generation-matched cache-HIT fixture unrelated to rotation-staleness, so
# keep it pinned to the CURRENT ANALYSIS_VERSION here rather than let it rot
# stale (and start triggering real re-analysis / network calls) on every
# future version bump. The rotation-staleness tests below build their own
# explicitly-versioned copies instead of relying on this module-level value.
_PROD_CLIP_ASSIGNMENT["analysis"]["analysis_version"] = ANALYSIS_VERSION


def test_clip_analysis_uses_three_workers_and_preserves_assignment_order(monkeypatch) -> None:
    active = 0
    max_active = 0
    lock = Lock()
    assignments = [
        {"media_id": f"clip-{index}", "gcs_path": f"users/u/{index}.mp4"} for index in range(7)
    ]

    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_args: True)

    def _analyze(raw: dict, _pool: dict[str, MediaRef]) -> tuple[dict, MediaRef]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        sleep(0.02)
        with lock:
            active -= 1
        return raw, MediaRef(
            lane="clip",
            media_id=str(raw["media_id"]),
            gcs_path=str(raw["gcs_path"]),
            generation="1",
            kind="video",
            duration_s=2,
        )

    monkeypatch.setattr(proposal_build, "_analyze_clip_assignment", _analyze)

    results = proposal_build._analyze_clip_assignments(
        assignments,
        {},
        item_id=uuid.uuid4(),
        attempt_id="attempt-1",
        ownership_epoch=0,
    )

    assert results is not None
    assert max_active == proposal_build._CLIP_ANALYSIS_CONCURRENCY
    assert [ref.media_id for _entry, ref in results] == [
        "clip-0",
        "clip-1",
        "clip-2",
        "clip-3",
        "clip-4",
        "clip-5",
        "clip-6",
    ]


def test_clip_analysis_stops_submitting_after_a_terminal_failure(monkeypatch) -> None:
    calls: list[str] = []
    assignments = [
        {"media_id": f"clip-{index}", "gcs_path": f"users/u/{index}.mp4"} for index in range(6)
    ]

    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_args: True)

    def _analyze(raw: dict, _pool: dict[str, MediaRef]) -> tuple[dict, MediaRef]:
        calls.append(str(raw["media_id"]))
        if raw["media_id"] == "clip-1":
            raise AssetUnreadableError("unreadable")
        sleep(0.02)
        return raw, MediaRef(
            lane="clip",
            media_id=str(raw["media_id"]),
            gcs_path=str(raw["gcs_path"]),
            generation="1",
            kind="video",
            duration_s=2,
        )

    monkeypatch.setattr(proposal_build, "_analyze_clip_assignment", _analyze)

    with pytest.raises(AssetUnreadableError, match="unreadable"):
        proposal_build._analyze_clip_assignments(
            assignments,
            {},
            item_id=uuid.uuid4(),
            attempt_id="attempt-1",
            ownership_epoch=0,
        )

    assert "clip-1" in calls
    assert set(calls).issubset({"clip-0", "clip-1", "clip-2"})


def test_clip_analysis_failure_does_not_wait_for_sibling_workers(monkeypatch) -> None:
    release_siblings = Event()
    siblings_started = Event()
    assignments = [
        {"media_id": f"clip-{index}", "gcs_path": f"users/u/{index}.mp4"} for index in range(3)
    ]

    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_args: True)

    def _analyze(raw: dict, _pool: dict[str, MediaRef]) -> tuple[dict, MediaRef]:
        if raw["media_id"] == "clip-1":
            siblings_started.wait(timeout=1)
            raise AssetUnreadableError("unreadable")
        siblings_started.set()
        release_siblings.wait(timeout=1)
        return raw, MediaRef(
            lane="clip",
            media_id=str(raw["media_id"]),
            gcs_path=str(raw["gcs_path"]),
            generation="1",
            kind="video",
            duration_s=2,
        )

    monkeypatch.setattr(proposal_build, "_analyze_clip_assignment", _analyze)

    with pytest.raises(AssetUnreadableError, match="unreadable"):
        proposal_build._analyze_clip_assignments(
            assignments,
            {},
            item_id=uuid.uuid4(),
            attempt_id="attempt-1",
            ownership_epoch=0,
        )

    release_siblings.set()


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def one_or_none(self):
        return self._row

    def all(self):
        return self._rows

    def scalars(self):
        return self._rows

    def scalar_one(self):
        return len(self._rows)


class _Db:
    def __init__(self, result: _Result | None = None):
        self.result = result or _Result()
        self.commits = 0

    def execute(self, _query):
        return self.result

    def commit(self):
        self.commits += 1

    def flush(self) -> None:
        pass


def _proposal(
    *,
    attempt_id: str = "attempt-1",
    status: str = "analyzing",
    brief: ProposalBrief | None = None,
    approval_mode: str | None = None,
) -> dict:
    return EditProposal(
        proposal_version=1,
        generation_attempt_id=attempt_id,
        status=status,
        approval_mode=approval_mode,
        brief=brief or ProposalBrief(),
    ).model_dump(mode="json")


def test_attempt_fence_requires_exact_epoch_attempt_and_active_status(monkeypatch) -> None:
    row = SimpleNamespace(edit_proposal=_proposal(), ownership_epoch=7)
    db = _Db(_Result(row=row))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    item_id = uuid.uuid4()

    assert proposal_build._attempt_is_active(item_id, "attempt-1", 7) is True
    assert proposal_build._attempt_is_active(item_id, "stale-attempt", 7) is False
    assert proposal_build._attempt_is_active(item_id, "attempt-1", 8) is False
    row.edit_proposal = _proposal(status="drafting")
    assert proposal_build._attempt_is_active(item_id, "attempt-1", 7) is False


def test_attempt_wants_auto_finalize_reads_approval_mode_off_the_row(monkeypatch) -> None:
    """P2-6: auto_finalize is derived from the persisted envelope, not a task

    kwarg — this is the exact read that replaces it.
    """

    row = SimpleNamespace(edit_proposal=_proposal(approval_mode="auto"), ownership_epoch=7)
    db = _Db(_Result(row=row))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    item_id = uuid.uuid4()

    assert proposal_build._attempt_wants_auto_finalize(item_id, "attempt-1", 7) is True
    # Wrong attempt (superseded/duplicate), wrong epoch, or a manual
    # (non-"auto") attempt must all read False.
    assert proposal_build._attempt_wants_auto_finalize(item_id, "stale-attempt", 7) is False
    assert proposal_build._attempt_wants_auto_finalize(item_id, "attempt-1", 8) is False
    row.edit_proposal = _proposal(approval_mode=None)
    assert proposal_build._attempt_wants_auto_finalize(item_id, "attempt-1", 7) is False

    row.edit_proposal = _proposal(approval_mode="auto", status="briefing")
    row.edit_proposal["guidance"] = {
        "state": "awaiting_direction_confirmation",
        "provenance": "ai_inferred",
        "hypothesis": {
            "direction": "fast_montage",
            "pace": "fast",
            "duration_s": 15,
            "text_density": "minimal",
            "audio_role": "music_led",
            "rationale": "A fast montage is my first guess.",
            "buildability_warnings": [],
        },
        "fingerprint": "a" * 64,
    }
    # Legacy direction-review rows are still automatic attempts; the worker
    # must be able to finish them after Generate resumes the state.
    assert proposal_build._attempt_wants_auto_finalize(item_id, "attempt-1", 7) is True


def test_pool_refs_rejects_asset_owned_by_another_user() -> None:
    owner_id = uuid.uuid4()
    foreign = SimpleNamespace(user_id=uuid.uuid4())
    db = _Db(_Result(rows=[foreign]))
    item = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(PermissionError, match="owner mismatch"):
        proposal_build._pool_refs(db, item, owner_id)


def test_pending_registered_asset_retries_same_proposal_attempt(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        idea="Corfu",
        theme="Travel",
        clip_assignments=[],
        edit_proposal=_proposal(),
    )
    pending = SimpleNamespace(user_id=owner_id, status="analyzing")
    db = _Db(_Result(rows=[pending]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(
        proposal_build,
        "_locked_item",
        lambda _db, _item_id, _epoch: (item, owner_id),
    )
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: nullcontext(),
    )

    with pytest.raises(Retry):
        proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "analyzing"
    assert db.commits == 0


def test_analysis_merge_preserves_concurrent_clip_editorial_changes() -> None:
    analyzed = [
        {
            "media_id": "clip-1",
            "gcs_path": "users/u/plan/i/corfu.mp4",
            "shot_id": "old-shot",
            "user_note": "old note",
            "machine_matched": True,
            "generation": "42",
            "kind": "video",
            "duration_s": 8.5,
            "aspect": 0.5625,
            "analysis": {"subject": "coast"},
        }
    ]
    current = [
        {
            "media_id": "clip-1",
            "gcs_path": "users/u/plan/i/corfu.mp4",
            "shot_id": "new-shot",
            "user_note": "creator changed this",
            "machine_matched": False,
        }
    ]

    merged = proposal_build._merge_analyzed_assignments(current, analyzed)

    assert merged is not None
    assert merged[0]["shot_id"] == "new-shot"
    assert merged[0]["user_note"] == "creator changed this"
    assert merged[0]["machine_matched"] is False
    assert merged[0]["generation"] == "42"
    assert merged[0]["analysis"] == {"subject": "coast"}


def test_clip_video_analysis_uses_privacy_safe_reuse_boundary(monkeypatch) -> None:
    media_id = str(uuid.uuid4())
    generation = "42"
    path = "users/u/plan/i/corfu.mov"
    video_analysis = []

    def _analyze(local_path: str):
        video_analysis.append(local_path)
        return {"subject": "Corfu coast"}, 0.5625, 4.0, (720, 1280)

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _analyze)
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/quicktime", generation=generation),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *_a, **_kw: None)

    entry, ref = proposal_build._analyze_clip_assignment(
        {"media_id": media_id, "gcs_path": path},
        {},
    )

    assert len(video_analysis) == 1
    assert entry["analysis"]["subject"] == "Corfu coast"
    assert ref.media_id == media_id
    assert ref.generation == generation


def test_soft_timeout_persists_retryable_failure_before_reraising(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        idea="Corfu",
        theme="Travel",
        clip_assignments=[],
        edit_proposal=_proposal(),
    )
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(
        proposal_build,
        "_locked_item",
        lambda _db, _item_id, _epoch: (item, owner_id),
    )

    def _timeout(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(proposal_build, "_pool_refs", _timeout)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: nullcontext(),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "proposal_generation_timeout"
    assert persisted.failure.retryable is True
    assert db.commits == 1


def _prod_item(
    item_id: uuid.UUID,
    *,
    clip_assignments: list[dict] | None = None,
    approval_mode: str | None = None,
) -> SimpleNamespace:
    assignments = (
        clip_assignments if clip_assignments is not None else [dict(_PROD_CLIP_ASSIGNMENT)]
    )
    return SimpleNamespace(
        id=item_id,
        idea="Athens",
        theme="",
        clip_assignments=assignments,
        clip_gcs_paths=[str(a["gcs_path"]) for a in assignments if a.get("gcs_path")],
        edit_proposal=_proposal(brief=ProposalBrief(duration_s=24), approval_mode=approval_mode),
    )


def test_transient_pool_video_analysis_error_retries_instead_of_wedging(monkeypatch) -> None:
    """Regression for the prod wedge on item 85d1de16-ba11-4533-9290-927a45819cd3.

    Root cause: _analyze_clip_assignment's cache-miss path calls
    analyze_pool_video, a raw Gemini call outside the Agent framework (no
    agent_run row on failure — matches the prod evidence of zero agent_run
    rows in the failure window). A transient AnalysisTemporarilyUnavailableError
    used to propagate straight through the loop to draft_edit_proposal's outer
    blanket `except Exception`, permanently marking the proposal "failed" with
    a retryable=True failure that never actually retried anything — wedging
    the creator forever. It must now trigger a real Celery retry instead.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id)
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    # Force a cache MISS (generation differs from the persisted "1787000010652201")
    # so _analyze_clip_assignment re-downloads and re-analyzes the clip.
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/quicktime", generation="999999999"),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *a, **kw: None)

    def _boom(_local_path):
        raise AnalysisTemporarilyUnavailableError("video analysis provider failed")

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _boom)

    with pytest.raises(Retry):
        proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "analyzing"  # never permanently wedged
    assert db.commits == 0


def test_asset_unreadable_error_fails_non_retryable_with_admin_only_detail(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id)
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/quicktime", generation="999999999"),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *a, **kw: None)

    def _boom(_local_path):
        raise AssetUnreadableError("video could not be decoded")

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _boom)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "media_unreadable"
    assert persisted.failure.retryable is False
    assert persisted.failure.detail is not None
    assert "AssetUnreadableError" in persisted.failure.detail
    # The admin-only diagnostic must never leak into user-facing copy.
    assert "AssetUnreadableError" not in persisted.failure.message
    assert db.commits == 1


def test_unexpected_exception_persists_admin_only_detail(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id)
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )

    def _boom(*_a, **_kw):
        raise ValueError("boom: unexpected shape")

    monkeypatch.setattr(proposal_build, "_pool_refs", _boom)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "proposal_generation_failed"
    assert persisted.failure.detail is not None
    assert "ValueError" in persisted.failure.detail
    assert "boom" in persisted.failure.detail
    assert "boom" not in persisted.failure.message
    assert db.commits == 1


def test_feasible_guided_duration_sums_video_and_credits_images() -> None:
    video = SimpleNamespace(kind="video", duration_s=6.768333)
    image = SimpleNamespace(kind="image", duration_s=None)
    assert proposal_build.feasible_guided_duration_s([video]) == pytest.approx(6.768333)
    assert proposal_build.feasible_guided_duration_s([video, image]) == pytest.approx(
        6.768333 + proposal_build._IMAGE_FEASIBLE_CREDIT_S
    )


def test_adapt_target_duration_clamps_to_feasible_footage() -> None:
    # 24s brief + 6.77s footage -> adapted target is bounded by the footage.
    adapted = proposal_build.adapt_target_duration_s(24, 6.768333)
    assert adapted <= 6
    assert adapted >= proposal_build.MIN_GUIDED_DURATION_S
    # Never exceeds the creator's requested duration either.
    assert proposal_build.adapt_target_duration_s(5, 40.0) == 5


def test_infeasible_footage_skips_agent_and_fails_with_actionable_message(monkeypatch) -> None:
    """A video shorter than the renderer's own min_moment_s (1.4s) earns ZERO

    feasibility credit (P2-1b, 2026-08-18 adversarial review) — it can never
    be its own legible beat moment, so crediting it (even at the image floor)
    would misrepresent what the footage can actually support.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    short = dict(_PROD_CLIP_ASSIGNMENT)
    short["duration_s"] = 1.0
    item = _prod_item(item_id, clip_assignments=[short])
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    # Cache HIT (generation unchanged) — reuses the persisted analysis + the
    # 1.0s duration without ever calling analyze_pool_video.
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )

    def _boom_client():
        raise AssertionError("the guided-edit agent must not run for infeasible footage")

    monkeypatch.setattr("app.agents._model_client.default_client", _boom_client)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "guided_edit_infeasible"
    assert "0.0" in persisted.failure.message
    assert db.commits == 1


def test_video_below_min_moment_earns_zero_credit_not_image_credit() -> None:
    zero_duration = SimpleNamespace(kind="video", duration_s=None)
    zero_flag = SimpleNamespace(kind="video", duration_s=0.0)
    too_short = SimpleNamespace(kind="video", duration_s=1.0)
    assert proposal_build.feasible_guided_duration_s([zero_duration]) == 0.0
    assert proposal_build.feasible_guided_duration_s([zero_flag]) == 0.0
    assert proposal_build.feasible_guided_duration_s([too_short]) == 0.0


def test_guided_feasibility_threshold_scales_with_media_count_floored_at_min() -> None:
    # 1-2 media: renderer minimum (1.4s each) never exceeds the agent's own
    # Pydantic ge=3 floor, so the threshold stays pinned at MIN_GUIDED_DURATION_S.
    assert proposal_build.guided_feasibility_threshold_s(1) == 3
    assert proposal_build.guided_feasibility_threshold_s(2) == 3
    # 3+ media: capped at 3 sources (minimum_required_sources' <=3 case uses
    # every source in one story) -> 1.4 * 3 = 4.2, regardless of how many more.
    assert proposal_build.guided_feasibility_threshold_s(3) == pytest.approx(4.2)
    assert proposal_build.guided_feasibility_threshold_s(10) == pytest.approx(4.2)


def test_many_too_short_videos_no_longer_overestimated_via_image_credit_bug() -> None:
    """Four 0.8s clips (3.2s raw total) cleared the OLD flat 3s floor even

    though not one of them is individually usable as its own beat moment —
    each was silently credited the FULL _IMAGE_FEASIBLE_CREDIT_S (1.4s) under
    the pre-P2-1 bug, overestimating total feasibility to 5.6s. Each now
    correctly earns zero credit (below _RENDERER_MIN_MOMENT_S), so the whole
    set is correctly infeasible.
    """

    media = [SimpleNamespace(kind="video", duration_s=0.8) for _ in range(4)]
    feasible = proposal_build.feasible_guided_duration_s(media)
    threshold = proposal_build.guided_feasibility_threshold_s(len(media))
    assert feasible == 0.0
    assert feasible < threshold


def test_feasibility_credits_only_the_usable_videos_in_a_mixed_set() -> None:
    usable = SimpleNamespace(kind="video", duration_s=5.0)
    too_short = SimpleNamespace(kind="video", duration_s=0.5)
    image = SimpleNamespace(kind="image", duration_s=None)
    feasible = proposal_build.feasible_guided_duration_s([usable, too_short, image])
    assert feasible == pytest.approx(5.0 + proposal_build._IMAGE_FEASIBLE_CREDIT_S)


class _FakeBeat:
    def __init__(self, media_ids: list[str]) -> None:
        self.topic = "Acropolis"
        self.thought = "Ancient stone stands tall against the sky."
        self.media_ids = media_ids
        self.layout = "fullscreen"
        self.duration_s = 6.0


class _FakeAgentOutput:
    def __init__(self, media_ids: list[str], *, duration_s: int = 6) -> None:
        self.title = "Athens in a moment"
        self.duration_s = duration_s
        self.story_beats = [_FakeBeat(media_ids)]


@pytest.mark.parametrize(("clip_count", "asset_count"), [(45, 58), (50, 100)])
def test_main_creator_large_shape_repairs_agent_refs_and_persists_all_media(
    monkeypatch,
    clip_count: int,
    asset_count: int,
) -> None:
    """The production failure and maximum accepted media shapes reach Kria intact."""

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    assignments = [
        {
            "media_id": f"clip-{index}",
            "gcs_path": f"users/u/plan/{item_id}/clips/{index}.mp4",
        }
        for index in range(clip_count)
    ]
    item = _prod_item(item_id, clip_assignments=assignments, approval_mode="auto")
    clip_refs = [
        MediaRef(
            lane="clip",
            media_id=str(assignment["media_id"]),
            gcs_path=str(assignment["gcs_path"]),
            generation="1",
            kind="video",
            duration_s=2,
        )
        for assignment in assignments
    ]
    pool_refs = [
        MediaRef(
            lane="asset",
            media_id=f"asset-{index}",
            gcs_path=f"users/u/plan/{item_id}/pool/{index}.jpg",
            generation="1",
            kind="image",
        )
        for index in range(asset_count)
    ]
    db = _Db(_Result(rows=[SimpleNamespace(user_id=owner_id, status="ready") for _ in pool_refs]))

    @contextmanager
    def _session():
        yield db

    captured_media: list[object] = []
    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(proposal_build, "_pool_refs", lambda *_a, **_kw: pool_refs)
    monkeypatch.setattr(
        proposal_build,
        "_analyze_clip_assignments",
        lambda clip_assignments, *_a, **_kw: list(zip(clip_assignments, clip_refs, strict=True)),
    )
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)

    def _run_agent(_self, input):  # noqa: ANN001, A002
        captured_media[:] = input.media
        from app.agents.edit_proposal import EditProposalAgent

        return EditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(
                {
                    "title": "A few moments",
                    "duration_s": 24,
                    "story_beats": [
                        {
                            "topic": "Opening",
                            "thought": "A clear opening sets the visual rhythm.",
                            "media_ids": ["unknown-a"],
                            "duration_s": 8,
                        },
                        {
                            "topic": "Details",
                            "thought": "Small details give the sequence texture.",
                            "media_ids": ["unknown-b"],
                            "duration_s": 8,
                        },
                        {
                            "topic": "Closing",
                            "thought": "A final frame gives the edit a finish.",
                            "media_ids": ["unknown-c"],
                            "duration_s": 8,
                        },
                    ],
                }
            ),
            input,
        )

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run_agent)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.last_approved is not None
    assert len(captured_media) == clip_count + asset_count
    assert len(persisted.last_approved.snapshot.media) == clip_count + asset_count
    selected = {
        media_id
        for beat in persisted.last_approved.snapshot.story_beats
        for media_id in beat.media_ids
    }
    assert selected <= {ref.media_id for ref in persisted.last_approved.snapshot.media}
    assert len(selected) >= 7


def test_initial_draft_terminal_agent_failure_uses_renderer_validated_fallback(
    monkeypatch,
) -> None:
    from app.agents._runtime import TerminalError

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id, approval_mode="auto")
    item.edit_proposal = _proposal(
        brief=ProposalBrief(duration_s=6),
        approval_mode="auto",
    )
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    clip_ref = MediaRef(
        lane="clip",
        media_id=str(_PROD_CLIP_ASSIGNMENT["media_id"]),
        gcs_path=str(_PROD_CLIP_ASSIGNMENT["gcs_path"]),
        generation=str(_PROD_CLIP_ASSIGNMENT["generation"]),
        kind="video",
        duration_s=6.768333,
    )
    monkeypatch.setattr(
        proposal_build,
        "_analyze_clip_assignments",
        lambda assignments, *_a, **_kw: [(assignments[0], clip_ref)],
    )
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda *_a, **_kw: (_ for _ in ()).throw(TerminalError("malformed provider media ref")),
    )

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.last_approved is not None
    snapshot = persisted.last_approved.snapshot
    assert snapshot.title == "A few moments"
    assert {media_id for beat in snapshot.story_beats for media_id in beat.media_ids} == {
        _PROD_CLIP_ASSIGNMENT["media_id"]
    }


class _FakeFastAgentOutput:
    def __init__(self, media_id: str) -> None:
        self.title = "Athens in three seconds"
        self.duration_s = 3
        self.story_beats = []
        self.fast_cuts = [
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id=media_id,
                source_start_s=float(index),
                source_end_s=float(index + 1),
                output_duration_s=1,
                role="hook" if index == 0 else "payoff" if index == 2 else "build",
                beat_align=index > 0,
            )
            for index in range(3)
        ]


def test_fast_cut_program_persists_with_legacy_compatibility_beats(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id)
    item.edit_proposal = _proposal(
        brief=ProposalBrief(
            direction="fast_montage",
            goal="Lead with the strongest Athens moment",
            pace="fast",
            duration_s=3,
        )
    )
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda self, input: _FakeFastAgentOutput(_PROD_CLIP_ASSIGNMENT["media_id"]),  # noqa: ARG005
    )

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "draft"
    assert persisted.draft is not None
    assert [cut.cut_id for cut in persisted.draft.fast_cuts or []] == [
        "cut-0",
        "cut-1",
        "cut-2",
    ]
    assert len(persisted.draft.story_beats) == 1
    assert persisted.draft.story_beats[0].thought == ""


def test_agent_output_longer_than_feasible_footage_is_rejected(monkeypatch) -> None:
    """The agent's +/-5s tolerance (EditProposalAgent.parse) checks output

    against the TARGET fed to it, not against real footage. A target floored
    by MIN_GUIDED_DURATION_S could still let a within-tolerance output exceed
    what the footage actually supports — draft_edit_proposal must reject that
    independently of the agent's own tolerance check (P2-1a).
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    clip = dict(_PROD_CLIP_ASSIGNMENT)
    clip["duration_s"] = 10.0
    item = _prod_item(item_id, clip_assignments=[clip])
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)
    # feasible=10.0 -> target=10, but the agent claims a 15s story anyway
    # (within its own +/-5s tolerance of the 10s target it was given).
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda self, input: _FakeAgentOutput(  # noqa: A002
            [_PROD_CLIP_ASSIGNMENT["media_id"]], duration_s=15
        ),
    )

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "guided_edit_infeasible"
    assert persisted.draft is None  # the oversized snapshot was never persisted
    assert db.commits == 2  # analyzing -> drafting, then drafting -> failed


def _auto_finalize_common_mocks(monkeypatch, item, owner_id) -> None:
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    # P2-6: auto_finalize is derived from the persisted envelope
    # (approval_mode="auto") via a cheap unlocked pre-read, not a task kwarg.
    # These fakes don't wire a real DB shape for that pre-read's raw SQL
    # query, so pin it directly — item.approval_mode is already "auto" on
    # every fixture that reaches this helper.
    monkeypatch.setattr(proposal_build, "_attempt_wants_auto_finalize", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )  # cache hit — reuses the persisted analysis, no download/analyze call
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: None)


def test_auto_finalize_success_approves_auto_and_dispatches_after_commit(monkeypatch) -> None:
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id, approval_mode="auto")
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    _auto_finalize_common_mocks(monkeypatch, item, owner_id)
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda self, input: _FakeAgentOutput(  # noqa: A002
            [_PROD_CLIP_ASSIGNMENT["media_id"]]
        ),
    )

    dispatch_calls = []

    def _fake_dispatch(
        item_id_arg,
        epoch,
        *,
        bypass_guided_edit_gate=False,
        creator_guided_attempt_id=None,
    ):
        dispatch_calls.append(
            (item_id_arg, epoch, bypass_guided_edit_gate, creator_guided_attempt_id)
        )
        return SimpleNamespace(outcome="dispatched")

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.approval_mode == "auto"
    assert persisted.last_approved is not None
    assert persisted.last_approved.approval_mode == "auto"
    assert persisted.design_fallback is None
    assert dispatch_calls == [(str(item_id), 0, False, "attempt-1")]


def test_auto_finalize_dispatch_failure_leaves_approved_no_wedge(monkeypatch) -> None:
    """If dispatch fails after auto-approval, the proposal stays approved —

    the next manual Generate click dispatches directly instead of re-running
    auto-design or wedging the creator.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id, approval_mode="auto")
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    _auto_finalize_common_mocks(monkeypatch, item, owner_id)
    monkeypatch.setattr(
        "app.agents.edit_proposal.EditProposalAgent.run",
        lambda self, input: _FakeAgentOutput(  # noqa: A002
            [_PROD_CLIP_ASSIGNMENT["media_id"]]
        ),
    )
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        lambda *_a, **_kw: SimpleNamespace(outcome="publish_failed"),
    )

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.approval_mode == "auto"


def test_auto_finalize_infeasible_footage_falls_back_to_montage_clip_only(monkeypatch) -> None:
    """guided-fail (infeasible footage) + zero registered pool assets ->

    legacy clip render dispatched anyway (bypass_guided_edit_gate=True) with
    design_fallback persisted.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    short = dict(_PROD_CLIP_ASSIGNMENT)
    short["duration_s"] = 1.0
    item = _prod_item(item_id, clip_assignments=[short], approval_mode="auto")
    db = _Db(_Result(rows=[]))  # zero pool assets

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    _auto_finalize_common_mocks(monkeypatch, item, owner_id)

    def _boom_client():
        raise AssertionError("the guided-edit agent must not run for infeasible footage")

    monkeypatch.setattr("app.agents._model_client.default_client", _boom_client)

    dispatch_calls = []

    def _fake_dispatch(
        item_id_arg,
        epoch,
        *,
        bypass_guided_edit_gate=False,
        creator_guided_attempt_id=None,
    ):
        dispatch_calls.append(
            (item_id_arg, epoch, bypass_guided_edit_gate, creator_guided_attempt_id)
        )
        return SimpleNamespace(outcome="dispatched")

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "guided_edit_infeasible"
    assert persisted.design_fallback == "guided_edit_infeasible"
    assert dispatch_calls == [(str(item_id), 0, True, "attempt-1")]


def test_main_creator_fail_closed_sentinel_blocks_generic_fallback(monkeypatch) -> None:
    """A confirmed Main Creator direction must fail closed across deploy skew.

    Rolling old workers only understand the pre-existing design_fallback
    truthiness check. The sentinel must therefore remain non-null and prevent
    the ordinary clip-only fallback without relying on a new task argument.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    short = dict(_PROD_CLIP_ASSIGNMENT)
    short["duration_s"] = 1.0
    item = _prod_item(item_id, clip_assignments=[short], approval_mode="auto")
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal is not None
    item.edit_proposal = proposal.model_copy(
        update={"design_fallback": "main_creator_fail_closed"}
    ).model_dump(mode="json")
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    _auto_finalize_common_mocks(monkeypatch, item, owner_id)
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        lambda *_a, **_kw: pytest.fail("generic fallback must not dispatch"),
    )

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.design_fallback == "main_creator_fail_closed"


def test_auto_finalize_infeasible_footage_with_pool_assets_never_falls_back(
    monkeypatch,
) -> None:
    """Pool assets exist -> never silently drop them behind a clip-only

    fallback (2026-08-15 incident invariant). The failure stays exactly as
    persisted; no dispatch is attempted at all.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    short = dict(_PROD_CLIP_ASSIGNMENT)
    short["duration_s"] = 1.0
    item = _prod_item(item_id, clip_assignments=[short], approval_mode="auto")
    pool_asset = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        status="ready",
        gcs_path="users/u/plan/i/pool/photo.jpg",
        gcs_generation="7",
        kind="image",
        source_filename="photo.jpg",
        duration_s=None,
        aspect=None,
        content_hash=None,
        user_context="",
        analysis={},
        created_at=None,
    )
    db = _Db(_Result(rows=[pool_asset]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    _auto_finalize_common_mocks(monkeypatch, item, owner_id)

    def _boom_client():
        raise AssertionError("the guided-edit agent must not run for infeasible footage")

    monkeypatch.setattr("app.agents._model_client.default_client", _boom_client)

    dispatch_calls = []

    def _fake_dispatch(*args, **kwargs):
        dispatch_calls.append((args, kwargs))
        return SimpleNamespace(outcome="dispatched")

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-1", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failure is not None
    assert persisted.failure.code == "guided_edit_infeasible"
    assert persisted.failure.retryable is True
    assert persisted.design_fallback is None
    assert dispatch_calls == []


def test_duplicate_task_invocation_while_a_newer_attempt_is_active_is_a_no_op(
    monkeypatch,
) -> None:
    """A second draft_edit_proposal invocation carrying a STALE attempt_id

    (e.g. a redelivered/duplicate Celery message, or an auto-design attempt
    racing a manual one) must never touch state or dispatch anything once a
    newer attempt already owns the proposal.
    """

    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    item = _prod_item(item_id, approval_mode="auto")
    # The envelope's live attempt is "attempt-current" — a different attempt
    # than the one this task invocation carries.
    current = parse_edit_proposal(item.edit_proposal)
    assert current is not None
    item.edit_proposal = current.model_copy(
        update={"generation_attempt_id": "attempt-current"}
    ).model_dump(mode="json")
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    # Force "wants auto-finalize" True even though the item's approval_mode
    # is "auto" too — the real safety net under test is
    # _dispatch_after_auto_design's OWN generation_attempt_id re-check, which
    # must catch this even when auto_finalize is (correctly or not)
    # affirmatively true for this stale invocation.
    monkeypatch.setattr(proposal_build, "_attempt_wants_auto_finalize", lambda *_a, **_kw: True)

    dispatch_calls = []
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        lambda *a, **kw: dispatch_calls.append((a, kw)) or SimpleNamespace(outcome="dispatched"),
    )

    proposal_build.draft_edit_proposal.run(str(item_id), "attempt-stale-duplicate", 0)

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.generation_attempt_id == "attempt-current"
    assert persisted.status == "analyzing"  # untouched by the stale invocation
    assert db.commits == 0
    assert dispatch_calls == []


# ── clip-lane rotation-aware cache staleness (autoplace ANALYSIS_VERSION 6) ────


def test_clip_assignment_reanalyzes_rotation_naive_cached_analysis(monkeypatch) -> None:
    """A generation-matched cache hit whose analysis is pre-v6 (rotation-naive)
    must be treated as a MISS, not reused verbatim — otherwise bumping
    ANALYSIS_VERSION alone never re-derives display dims for already-cached
    clip-lane rows."""

    calls: list[str] = []

    def _analyze(local_path: str):
        calls.append(local_path)
        return (
            {"subject": "coast", "source": "clip_metadata", "analysis_version": ANALYSIS_VERSION},
            0.5625,
            6.768333,
            (1080, 1920),
        )

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _analyze)
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *_a, **_kw: None)

    # Generation matches _PROD_CLIP_ASSIGNMENT but its analysis carries
    # analysis_version=5, pre-dating the rotation-aware display dims fix.
    stale_entry = dict(_PROD_CLIP_ASSIGNMENT)
    stale_entry["analysis"] = {**stale_entry["analysis"], "analysis_version": 5}

    entry, ref = proposal_build._analyze_clip_assignment(stale_entry, {})

    assert len(calls) == 1
    assert entry["analysis"]["analysis_version"] == ANALYSIS_VERSION
    assert ref.analysis["analysis_version"] == ANALYSIS_VERSION


def test_clip_assignment_reuses_current_version_cached_analysis(monkeypatch) -> None:
    """The mirror case: a generation-matched cache hit already at the current
    ANALYSIS_VERSION must NOT trigger a re-download/re-analyze."""

    fresh_entry = dict(_PROD_CLIP_ASSIGNMENT)
    fresh_entry["analysis"] = {
        **fresh_entry["analysis"],
        "analysis_version": ANALYSIS_VERSION,
    }
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(
            content_type="video/quicktime", generation="1787000010652201"
        ),
    )

    def _boom(_local_path):
        raise AssertionError("must not re-analyze a current-version cached row")

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _boom)

    entry, ref = proposal_build._analyze_clip_assignment(fresh_entry, {})

    assert entry["analysis"]["analysis_version"] == ANALYSIS_VERSION
    assert ref.analysis["subject"] == "Acropolis of Athens"


def test_keyless_clip_analysis_stamps_version_so_it_never_reanalyzes(monkeypatch) -> None:
    """Trap: without stamping analysis_version on a keyless (no-Gemini-key)
    result, the persisted analysis dict has no analysis_version key at all,
    which analysis_is_stale() reads as version 1 — forever stale — causing
    every subsequent draft attempt to re-download and re-probe the clip."""

    media_id = str(uuid.uuid4())
    path = "users/u/plan/i/corfu.mov"
    generation = "42"
    calls: list[str] = []

    def _analyze(local_path: str):
        calls.append(local_path)
        return None, 0.5625, 4.0, (1080, 1920)  # keyless: no Gemini analysis dict

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", _analyze)
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/quicktime", generation=generation),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *_a, **_kw: None)

    entry, _ref = proposal_build._analyze_clip_assignment(
        {"media_id": media_id, "gcs_path": path},
        {},
    )

    assert len(calls) == 1
    assert entry["analysis"]["analysis_version"] == ANALYSIS_VERSION

    # Re-run with the persisted entry as the incoming cache row: same
    # generation, now-stamped analysis_version -> must be a cache HIT.
    entry2, _ref2 = proposal_build._analyze_clip_assignment(entry, {})

    assert len(calls) == 1
    assert entry2["analysis"]["analysis_version"] == ANALYSIS_VERSION
