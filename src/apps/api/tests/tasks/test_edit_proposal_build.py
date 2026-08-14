from __future__ import annotations

import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import Retry

import app.tasks.edit_proposal_build as proposal_build
from app.schemas.edit_proposal import EditProposal, ProposalBrief, parse_edit_proposal


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


def _proposal(*, attempt_id: str = "attempt-1", status: str = "analyzing") -> dict:
    return EditProposal(
        proposal_version=1,
        generation_attempt_id=attempt_id,
        status=status,
        brief=ProposalBrief(),
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
