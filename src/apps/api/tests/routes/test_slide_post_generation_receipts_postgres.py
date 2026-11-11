# ruff: noqa: F811
"""Real transaction journey; only broker publication is replaced, never paid work.

Failure cases fixed before implementation: lost-response retry after terminal ready,
old-version retry after newer edits/Jobs, UUID reuse, deleted Job, atomic rollback,
concurrent duplicate mint, and immutable first-render input versus explicit rebuild.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.database import sync_session
from app.models import Job, PlanItem, PlanItemAsset
from tests.routes.test_slide_post_binding_save_postgres import (  # noqa: F401
    _flags,
    client,
    seeded,
)


@pytest.fixture(autouse=True)
def _receipt_flags(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)


@pytest.fixture
def publishes(monkeypatch, seeded):
    calls = []
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync",
        lambda _task, job_id, **_kw: calls.append(job_id),
    )
    # Worker validates the project-scoped media path, not the legacy pool fixture.
    with sync_session() as db:
        for asset in db.scalars(
            select(PlanItemAsset).where(PlanItemAsset.plan_item_id == seeded.item_id)
        ):
            asset.gcs_path = f"users/{seeded.user_id}/plan/{seeded.item_id}/{asset.id}.jpg"
        db.commit()
    return calls


def _post(client, seeded, key, version=1, expected_job=None):
    return client.post(
        f"/plan-items/{seeded.item_id}/slide-post/generation-requests",
        json={
            "expected_version": version,
            "client_request_id": str(key),
            "expected_current_job_id": str(expected_job) if expected_job else None,
        },
    )


@pytest.mark.parametrize("status", ["variants_ready", "processing_failed", "queued"])
def test_lost_response_retry_returns_original_receipt_after_newer_target(
    client, seeded, publishes, status
):
    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    receipt = first.json()["generation_receipt"]
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(receipt["job_id"]))
        job.status = status
        job.assembly_plan = {"variants": []}
        item = db.get(PlanItem, seeded.item_id)
        item.slide_post = {**item.slide_post, "version": 2, "caption": "Newer"}
        item.current_job_id = None
        db.commit()
    replay = _post(client, seeded, key)
    assert replay.status_code == 200, replay.text
    recovered = replay.json()["generation_receipt"]
    assert recovered["job_id"] == receipt["job_id"]
    assert recovered["draft_version"] == 1
    assert recovered["is_current_target"] is False
    assert len(publishes) == 1
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).current_job_id is None


def test_parallel_duplicate_and_payload_mismatch(client, seeded, publishes):
    key = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: _post(client, seeded, key), range(2)))
    assert [r.status_code for r in replies] == [200, 200]
    assert len({r.json()["generation_receipt"]["job_id"] for r in replies}) == 1
    assert len(publishes) == 1
    assert _post(client, seeded, key, version=2).status_code == 409


def test_first_render_uses_receipt_but_explicit_rebuild_reads_live_draft(client, seeded, publishes):
    from app.tasks.generative_build import _resolve_slide_post_draft_and_assets

    first = _post(client, seeded, uuid.uuid4())
    assert first.status_code == 200, first.text
    job_id = first.json()["generation_receipt"]["job_id"]
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        item.slide_post = {**item.slide_post, "version": 2, "caption": "Newer"}
        db.get(Job, uuid.UUID(job_id)).assembly_plan = {"variants": []}
        db.commit()
        initial = _resolve_slide_post_draft_and_assets(db, job_id, approved=True)[2]
        rebuild = _resolve_slide_post_draft_and_assets(db, job_id, approved=False)[2]
        assert (initial.version, initial.caption) == (1, "Five photos")
        assert (rebuild.version, rebuild.caption) == (2, "Newer")


def test_commit_then_process_loss_replays_unconfirmed_without_publish(
    client, seeded, publishes, monkeypatch
):
    from app.models import ContentPlan
    from app.services.slide_post_generation_receipts import (
        SlidePostGenerationRequest,
        dispatch_generation,
    )

    key = uuid.uuid4()

    class LostProcess(BaseException):
        pass

    def stopped(*_args, **_kwargs):
        raise LostProcess()

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", stopped)
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        epoch = db.get(ContentPlan, item.content_plan_id).ownership_epoch
    # A process-ending exception must not tear down TestClient's own event loop.
    # Exercise the real sync transaction directly, then recover through HTTP.
    with pytest.raises(LostProcess):
        dispatch_generation(
            SlidePostGenerationRequest(key, seeded.user_id, seeded.item_id, 1), epoch
        )
    replay = _post(client, seeded, key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["generation_receipt"]["dispatch_state"] == "unconfirmed"
    assert publishes == []


def test_receipt_insert_failure_rolls_back_job_and_target(client, seeded, publishes, monkeypatch):
    from app.models import SlidePostGenerationReceipt
    from app.tasks import content_plan_build

    key = uuid.uuid4()
    capture = content_plan_build.capture_receipt

    def fail_after_capture(*args, **kwargs):
        capture(*args, **kwargs)
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(content_plan_build, "capture_receipt", fail_after_capture)
    with pytest.raises(RuntimeError, match="injected before commit"):
        _post(client, seeded, key)
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).current_job_id is None
        assert db.get(SlidePostGenerationReceipt, key) is None
        assert list(db.scalars(select(Job).where(Job.content_plan_item_id == seeded.item_id))) == []
    assert publishes == []
    monkeypatch.setattr(content_plan_build, "capture_receipt", capture)
    assert _post(client, seeded, key).status_code == 200
    assert len(publishes) == 1


def test_deleted_job_and_cross_item_key_reuse_never_mint(client, seeded, publishes):
    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    job_id = uuid.UUID(first.json()["generation_receipt"]["job_id"])
    with sync_session() as db:
        db.get(PlanItem, seeded.item_id).current_job_id = None
        db.flush()
        db.delete(db.get(Job, job_id))
        db.commit()
    replay = _post(client, seeded, key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["generation_receipt"]["job_id"] == str(job_id)
    assert replay.json()["generation_receipt"]["job_status"] is None
    collision = client.post(
        f"/plan-items/{seeded.other_item_id}/slide-post/generation-requests",
        json={
            "expected_version": 1,
            "client_request_id": str(key),
            "expected_current_job_id": None,
        },
    )
    assert collision.status_code == 409, collision.text
    assert len(publishes) == 1


@pytest.mark.parametrize("field", ["gcs_generation", "gcs_path", "user_id", "media_status"])
def test_worker_rejects_changed_snapshot_input(client, seeded, publishes, field):
    from app.tasks.generative_build import (
        SlidePostPolicyError,
        _resolve_slide_post_draft_and_assets,
    )

    first = _post(client, seeded, uuid.uuid4())
    assert first.status_code == 200, first.text
    job_id = first.json()["generation_receipt"]["job_id"]
    with sync_session() as db:
        asset = db.get(PlanItemAsset, seeded.asset_ids[0])
        # No flush is required: exercise the actual resolver with a changed
        # identity-map row without introducing a foreign-key-invalid fixture.
        value = uuid.uuid4() if field == "user_id" else "changed"
        setattr(asset, field, value)
        with db.no_autoflush, pytest.raises(SlidePostPolicyError):
            _resolve_slide_post_draft_and_assets(db, job_id, approved=True)


def test_global_uuid_race_rolls_back_losing_job(client, seeded, publishes, monkeypatch):
    import copy
    import threading

    from app.models import ContentPlan, CreationThread
    from app.services.slide_post_generation_receipts import (
        ReceiptConflict,
        SlidePostGenerationRequest,
        dispatch_generation,
    )
    from app.tasks import content_plan_build
    from tests.routes.test_slide_post_binding_save_postgres import _seed_runtime_project

    owner2, thread2, _session2 = _seed_runtime_project()
    with sync_session() as db:
        first = db.get(PlanItem, seeded.item_id)
        second = db.get(PlanItem, db.get(CreationThread, thread2).active_plan_item_id)
        second.edit_format = "slides"
        draft = copy.deepcopy(first.slide_post)
        draft["brief_binding"] = None
        for ref in draft["slides"]:
            asset = PlanItemAsset(
                user_id=owner2,
                plan_item_id=second.id,
                kind="image",
                status="ready",
                media_status="ready",
                gcs_generation="7",
                gcs_path=f"users/{owner2}/plan/{second.id}/{uuid.uuid4()}.jpg",
            )
            db.add(asset)
            db.flush()
            ref["asset_id"] = str(asset.id)
        second.slide_post = draft
        key = uuid.uuid4()
        requests = [
            (
                SlidePostGenerationRequest(key, seeded.user_id, first.id, 1),
                db.get(ContentPlan, first.content_plan_id).ownership_epoch,
            ),
            (
                SlidePostGenerationRequest(key, owner2, second.id, 1),
                db.get(ContentPlan, second.content_plan_id).ownership_epoch,
            ),
        ]
        item_ids = [first.id, second.id]
        db.commit()
    barrier = threading.Barrier(2)
    capture = content_plan_build.capture_receipt

    def together(*args, **kwargs):
        row = capture(*args, **kwargs)
        barrier.wait(timeout=10)
        return row

    monkeypatch.setattr(content_plan_build, "capture_receipt", together)

    def run(pair):
        try:
            return dispatch_generation(*pair)
        except ReceiptConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, requests))
    assert results.count("conflict") == 1
    assert len(publishes) == 1
    with sync_session() as db:
        jobs = list(db.scalars(select(Job).where(Job.content_plan_item_id.in_(item_ids))))
        assert len(jobs) == 1
        targets = [db.get(PlanItem, item_id).current_job_id for item_id in item_ids]
        assert targets.count(None) == 1
        assert jobs[0].id in targets


def test_publish_exception_is_durable_and_retry_does_not_publish(
    client, seeded, publishes, monkeypatch
):
    calls = []

    def fail(_task, job_id, **_kwargs):
        calls.append(job_id)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator_sync", fail)
    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    receipt = first.json()["generation_receipt"]
    assert receipt["dispatch_state"] == "unconfirmed"
    assert receipt["job_status"] == "queued"
    second = _post(client, seeded, key)
    assert second.status_code == 200, second.text
    assert second.json()["generation_receipt"] == receipt
    assert len(calls) == 1


def test_required_key_and_unrelated_active_job_never_dispatch(client, seeded, publishes):
    path = f"/plan-items/{seeded.item_id}/slide-post/generation-requests"
    assert client.post(path, json={"expected_version": 1}).status_code == 422
    assert _post(client, seeded, uuid.uuid4(), version=2).status_code == 409
    assert publishes == []
    first = _post(client, seeded, uuid.uuid4())
    assert first.status_code == 200, first.text
    next_intent = _post(client, seeded, uuid.uuid4())
    assert next_intent.status_code == 409, next_intent.text
    assert len(publishes) == 1


def test_receipt_get_observes_same_version_successor_without_rewinding(client, seeded, publishes):
    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    original = first.json()["generation_receipt"]
    with sync_session() as db:
        db.get(Job, uuid.UUID(original["job_id"])).status = "variants_ready"
        db.commit()
    successor = _post(client, seeded, uuid.uuid4(), expected_job=original["job_id"])
    assert successor.status_code == 200, successor.text
    current_id = successor.json()["generation_receipt"]["job_id"]
    response = client.get(f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{key}")
    assert response.status_code == 200, response.text
    observed = response.json()
    for field in ("client_request_id", "item_id", "draft_version", "job_id"):
        assert observed[field] == original[field]
    assert observed["is_current_target"] is False
    assert observed["job_status"] == "variants_ready"
    assert len(publishes) == 2
    with sync_session() as db:
        assert str(db.get(PlanItem, seeded.item_id).current_job_id) == current_id


def test_receipt_get_survives_deleted_job_without_any_mutation(client, seeded, publishes):
    from app.models import SlidePostGenerationReceipt

    key = uuid.uuid4()
    first = _post(client, seeded, key)
    job_id = uuid.UUID(first.json()["generation_receipt"]["job_id"])
    with sync_session() as db:
        db.get(PlanItem, seeded.item_id).current_job_id = None
        db.flush()
        db.delete(db.get(Job, job_id))
        db.commit()
        before = dict(db.get(SlidePostGenerationReceipt, key).__dict__)
        before.pop("_sa_instance_state")
    response = client.get(f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{key}")
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] == str(job_id)
    assert response.json()["job_status"] is None
    assert response.json()["is_current_target"] is False
    assert len(publishes) == 1
    with sync_session() as db:
        after = dict(db.get(SlidePostGenerationReceipt, key).__dict__)
        after.pop("_sa_instance_state")
        assert after == before
        assert db.get(PlanItem, seeded.item_id).current_job_id is None


def test_receipt_get_checks_current_item_access_and_key_scope(client, seeded, publishes):
    from types import SimpleNamespace

    from app.auth import get_current_user
    from app.main import app

    key = uuid.uuid4()
    assert _post(client, seeded, key).status_code == 200
    path = f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{key}"
    assert (
        client.get(
            f"/plan-items/{seeded.other_item_id}/slide-post/generation-requests/{key}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{uuid.uuid4()}"
        ).status_code
        == 404
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    assert client.get(path).status_code == 404
    assert len(publishes) == 1


def test_rollout_gate_disables_capability_and_new_admission(client, seeded, publishes, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", False)
    state = client.get(f"/plan-items/{seeded.item_id}/slide-post")
    assert state.status_code == 200, state.text
    assert state.json()["generation_idempotency_version"] == 0
    assert _post(client, seeded, uuid.uuid4()).status_code == 409
    assert publishes == []
    with sync_session() as db:
        assert db.get(PlanItem, seeded.item_id).current_job_id is None


def test_new_uuid_cannot_replace_same_version_successor_from_stale_target(
    client, seeded, publishes
):
    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    job_id = uuid.UUID(first.json()["generation_receipt"]["job_id"])
    with sync_session() as db:
        db.get(Job, job_id).status = "variants_ready"
        db.commit()
    # Another device still thinks there is no Job, although version is unchanged.
    stale = _post(client, seeded, uuid.uuid4(), expected_job=None)
    assert stale.status_code == 409, stale.text
    assert len(publishes) == 1
    # The original request still observes its pinned receipt after target drift.
    assert _post(client, seeded, key, expected_job=None).status_code == 200
    # Reusing a key with a different original target is a payload mismatch.
    assert _post(client, seeded, key, expected_job=job_id).status_code == 409


def test_rollout_off_preserves_read_only_existing_receipt_observation(
    client, seeded, publishes, monkeypatch
):
    from app.config import settings

    key = uuid.uuid4()
    first = _post(client, seeded, key)
    assert first.status_code == 200, first.text
    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", False)
    assert _post(client, seeded, key).status_code == 200
    response = client.get(f"/plan-items/{seeded.item_id}/slide-post/generation-requests/{key}")
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] == first.json()["generation_receipt"]["job_id"]
    assert len(publishes) == 1
