from __future__ import annotations

import json
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import Retry

import app.tasks.edit_proposal_build as proposal_build
from app.schemas.edit_proposal import EditProposal, ProposalBrief, parse_edit_proposal
from app.tasks.autoplace import AnalysisTemporarilyUnavailableError, AssetUnreadableError

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


class _Db:
    def __init__(self, result: _Result | None = None):
        self.result = result or _Result()
        self.commits = 0

    def execute(self, _query):
        return self.result

    def commit(self):
        self.commits += 1


def _proposal(
    *, attempt_id: str = "attempt-1", status: str = "analyzing", brief: ProposalBrief | None = None
) -> dict:
    return EditProposal(
        proposal_version=1,
        generation_attempt_id=attempt_id,
        status=status,
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
    item_id: uuid.UUID, *, clip_assignments: list[dict] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        idea="Athens",
        theme="",
        clip_assignments=(
            clip_assignments if clip_assignments is not None else [dict(_PROD_CLIP_ASSIGNMENT)]
        ),
        edit_proposal=_proposal(brief=ProposalBrief(duration_s=24)),
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
    assert "1.0" in persisted.failure.message
    assert db.commits == 1
