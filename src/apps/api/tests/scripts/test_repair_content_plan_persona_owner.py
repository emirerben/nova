from __future__ import annotations

import importlib.util
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_SCRIPT = Path(__file__).parents[5] / "scripts" / "prod" / "repair_content_plan_persona_owner.py"
_SPEC = importlib.util.spec_from_file_location("repair_content_plan_persona_owner", _SCRIPT)
assert _SPEC and _SPEC.loader
repair = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(repair)


class _Scalars:
    def __init__(self, rows):  # noqa: ANN001
        self.rows = rows

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Result:
    def __init__(self, rows=None, *, rowcount: int = 0):  # noqa: ANN001
        self.rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return _Scalars(self.rows)


class _Session:
    def __init__(self, *, plan=None, results=None):  # noqa: ANN001
        self.plan = plan
        self.results = iter(results or [])
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return nullcontext()

    def get(self, _model, _row_id, **_kwargs):  # noqa: ANN001
        return self.plan

    def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return next(self.results)


def _snapshot() -> dict:
    return {
        "fingerprint": "safe-fingerprint",
        "items": [],
        "jobs": [],
        "seed_checks": [],
        "objects": [],
    }


def test_mutating_phase_requires_apply_and_matching_fingerprint(monkeypatch) -> None:
    snapshot = _snapshot()
    monkeypatch.setattr(repair, "_inventory", lambda: snapshot)
    fence = MagicMock()
    monkeypatch.setattr(repair, "_fence", fence)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "fence"])

    with pytest.raises(RuntimeError, match="mutating phases require"):
        repair.main()

    fence.assert_not_called()

    monkeypatch.setattr(
        sys,
        "argv",
        [str(_SCRIPT), "fence", "--apply", "--fingerprint", "wrong"],
    )
    with pytest.raises(RuntimeError, match="fingerprint"):
        repair.main()
    fence.assert_not_called()


def test_archive_does_not_delete_when_evidence_checksum_mismatches(monkeypatch) -> None:
    source = MagicMock()
    source_snapshot = {
        **_snapshot(),
        "objects": [
            {
                "path": "generative-jobs/job/output.mp4",
                "generation": "7",
                "size": 100,
                "md5": "expected",
                "crc32c": None,
            }
        ],
    }
    destination = SimpleNamespace(size=100, md5_hash="different", crc32c=None, reload=lambda: None)
    bucket = MagicMock()
    bucket.blob.return_value = source
    bucket.copy_blob.return_value = destination
    client = MagicMock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(repair, "_get_client", lambda: client)

    with pytest.raises(RuntimeError, match="content hash mismatch"):
        repair._archive_and_delete_outputs(source_snapshot)

    source.delete.assert_not_called()
    bucket.copy_blob.assert_called_once()
    assert bucket.copy_blob.call_args.kwargs["if_source_generation_match"] == 7


def test_verify_quiescence_fails_closed_when_celery_is_unavailable(monkeypatch) -> None:
    inspect = MagicMock()
    inspect.active.return_value = None
    inspect.reserved.return_value = {}
    inspect.scheduled.return_value = {}
    monkeypatch.setattr(repair.celery_app.control, "inspect", lambda timeout: inspect)

    with pytest.raises(RuntimeError, match="inspection is unavailable"):
        repair._verify_quiescence(_snapshot())


def test_seed_map_ignores_invalid_rows_and_fingerprint_is_stable() -> None:
    persona = SimpleNamespace(idea_seeds=[None, {}, {"id": "seed", "text": "idea"}])
    assert repair._seed_map(persona) == {"seed": {"id": "seed", "text": "idea"}}

    plan = SimpleNamespace(id="p", user_id="u")
    linked = SimpleNamespace(id="l", user_id="foreign")
    owner = SimpleNamespace(id="o")
    assert repair._fingerprint(plan, linked, owner) == repair._fingerprint(plan, linked, owner)


def test_fence_quarantines_epoch_and_uses_expected_status_cancellation(monkeypatch) -> None:
    plan_id, linked_id, owner_id, item_id, job_id = [uuid.uuid4() for _ in range(5)]
    plan = SimpleNamespace(
        id=plan_id,
        ownership_epoch=4,
        ownership_quarantined_at=None,
    )
    item = SimpleNamespace(id=item_id)
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready",
        finished_at=None,
    )
    snapshot = {
        **_snapshot(),
        "plan": {
            "id": str(plan_id),
            "linked_persona_id": str(linked_id),
            "owner_persona_id": str(owner_id),
        },
        "jobs": [{"id": str(job_id), "status": "variants_ready"}],
    }
    session = _Session(
        plan=plan,
        results=[
            _Result([SimpleNamespace(id=linked_id), SimpleNamespace(id=owner_id)]),
            _Result([item]),
            _Result([job]),
            _Result(rowcount=1),
        ],
    )
    monkeypatch.setattr(repair, "sync_session", lambda: session)

    repair._fence(snapshot)

    assert plan.ownership_epoch == 5
    assert plan.ownership_quarantined_at is not None
    update_params = session.statements[-1].compile().params
    assert update_params["status"] == "cancelled"
    assert update_params["failure_reason"] == "persona_owner_mismatch"


def test_fence_aborts_when_job_status_drifted(monkeypatch) -> None:
    plan_id, linked_id, owner_id, item_id, job_id = [uuid.uuid4() for _ in range(5)]
    plan = SimpleNamespace(id=plan_id, ownership_epoch=0, ownership_quarantined_at=None)
    job = SimpleNamespace(id=job_id, status="processing", finished_at=None)
    session = _Session(
        plan=plan,
        results=[
            _Result([SimpleNamespace(id=linked_id), SimpleNamespace(id=owner_id)]),
            _Result([SimpleNamespace(id=item_id)]),
            _Result([job]),
        ],
    )
    monkeypatch.setattr(repair, "sync_session", lambda: session)
    snapshot = {
        **_snapshot(),
        "plan": {
            "id": str(plan_id),
            "linked_persona_id": str(linked_id),
            "owner_persona_id": str(owner_id),
        },
        "jobs": [{"id": str(job_id), "status": "variants_ready"}],
    }

    with pytest.raises(RuntimeError, match="job status changed"):
        repair._fence(snapshot)

    assert plan.ownership_quarantined_at is None


def test_repair_moves_only_exact_seed_and_retains_quarantine(monkeypatch) -> None:
    plan_id, linked_id, owner_id, item_id, job_id = [uuid.uuid4() for _ in range(5)]
    quarantine = object()
    plan = SimpleNamespace(
        id=plan_id,
        user_id="owner-user",
        persona_id=linked_id,
        ownership_quarantined_at=quarantine,
    )
    linked = SimpleNamespace(
        id=linked_id,
        user_id="foreign-user",
        idea_seeds=[
            {"id": "move", "text": "Exact idea", "status": "pending"},
            {"id": "stay", "text": "Other idea", "status": "pending"},
        ],
    )
    owner = SimpleNamespace(id=owner_id, user_id="owner-user", idea_seeds=[])
    item = SimpleNamespace(
        id=item_id,
        source_idea_seed_id="move",
        idea="Exact idea",
        current_job_id=job_id,
    )
    job = SimpleNamespace(id=job_id, status="cancelled")
    mutation = _Session(
        plan=plan,
        results=[
            _Result([linked, owner]),
            _Result([item]),
            _Result([job]),
            _Result([]),
            _Result([]),
        ],
    )
    audit = _Session(results=[_Result([])])
    sessions = iter([mutation, audit])
    monkeypatch.setattr(repair, "sync_session", lambda: next(sessions))
    snapshot = {
        **_snapshot(),
        "plan": {
            "id": str(plan_id),
            "linked_persona_id": str(linked_id),
            "owner_persona_id": str(owner_id),
        },
    }

    repair._repair(snapshot)

    assert linked.idea_seeds == [{"id": "stay", "text": "Other idea", "status": "pending"}]
    assert owner.idea_seeds == [{"id": "move", "text": "Exact idea", "status": "in_plan"}]
    assert item.current_job_id is None
    assert plan.persona_id == owner_id
    assert plan.ownership_quarantined_at is quarantine


def test_repair_requires_durable_quarantine(monkeypatch) -> None:
    plan = SimpleNamespace(ownership_quarantined_at=None)
    monkeypatch.setattr(repair, "sync_session", lambda: _Session(plan=plan))

    with pytest.raises(RuntimeError, match="not durably quarantined"):
        repair._repair({**_snapshot(), "plan": {"id": str(uuid.uuid4())}})
