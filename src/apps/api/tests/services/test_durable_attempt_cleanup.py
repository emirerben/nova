from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.durable_attempt_cleanup as cleanup
from app import storage


def _job(plan: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        raw_storage_path="dev-user/raw.mp4",
        all_candidates={},
        probe_metadata=None,
        transcript=None,
        scene_cuts=None,
        assembly_plan={} if plan is None else plan,
    )


def test_source_receipt_is_precommitted_with_exact_attempt_prefix() -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    lease = datetime.now(UTC) + timedelta(minutes=30)

    receipt = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=lease,
    )

    assert receipt == {
        "copy_attempt_id": str(attempt_id),
        "prefix": (f"generative-jobs/{job.id}/sources/copy-attempts/{attempt_id}/"),
        "upload_state": "writing",
        "lease_expires_at": lease.isoformat(),
    }
    parsed = list(cleanup.iter_cleanup_receipts(job.assembly_plan, job=job))
    assert parsed == [
        cleanup.PrefixCleanupReceipt(
            locator=cleanup.CleanupReceiptLocator(
                field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
                receipt_id=str(attempt_id),
            ),
            prefix=receipt["prefix"],
            upload_state="writing",
            lease_expires_at=lease,
        )
    ]


def test_receipt_idempotence_does_not_consume_capacity_twice() -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    lease = datetime.now(UTC) + timedelta(minutes=30)

    first = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=lease,
    )
    second = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=lease,
    )

    assert first == second
    assert (
        len(
            job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
                cleanup.SOURCE_COPY_CLEANUP_FIELD
            ]
        )
        == 1
    )


def test_source_receipt_cap_fails_before_receipt_loss() -> None:
    job = _job()
    lease = datetime.now(UTC) + timedelta(minutes=30)
    for _ in range(cleanup.SOURCE_COPY_RECEIPT_CAP):
        cleanup.reserve_source_copy_cleanup(
            job.assembly_plan,
            job_id=job.id,
            copy_attempt_id=uuid.uuid4(),
            lease_expires_at=lease,
        )
    snapshot = copy.deepcopy(job.assembly_plan)

    with pytest.raises(cleanup.CleanupReceiptBackpressure):
        cleanup.reserve_source_copy_cleanup(
            job.assembly_plan,
            job_id=job.id,
            copy_attempt_id=uuid.uuid4(),
            lease_expires_at=lease,
        )

    assert job.assembly_plan == snapshot


def test_malformed_private_container_fails_closed() -> None:
    job = _job({cleanup.SPEECH_CLEANUP_INTERNAL_FIELD: []})

    assert cleanup.cleanup_debt_present(job.assembly_plan)
    with pytest.raises(cleanup.CleanupReceiptError):
        list(cleanup.iter_cleanup_receipts(job.assembly_plan, job=job))


def test_wrong_prefix_cannot_be_parsed_as_owned_receipt() -> None:
    job = _job()
    generation = str(uuid.uuid4())
    raw = {
        "generation": generation,
        "prefix": f"generative-jobs/{uuid.uuid4()}/render-generations/{generation}/",
        "upload_state": "closed",
        "lease_expires_at": datetime.now(UTC).isoformat(),
    }

    with pytest.raises(cleanup.CleanupReceiptError, match="does not match"):
        cleanup.parse_cleanup_receipt(
            job=job,
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            raw=raw,
        )


def test_existing_hex_render_generation_token_remains_byte_stable() -> None:
    job = _job()
    generation = uuid.uuid4().hex

    receipt = cleanup.new_render_generation_receipt(
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )

    assert receipt["generation"] == generation
    assert receipt["prefix"] == (f"generative-jobs/{job.id}/render-generations/{generation}/")


def test_remove_last_receipt_removes_sparse_index_keys() -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=datetime.now(UTC),
    )

    assert cleanup.remove_cleanup_receipt(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
            receipt_id=str(attempt_id),
        ),
    )

    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan
    assert not cleanup.cleanup_debt_present(job.assembly_plan)


def test_prune_empty_cleanup_keys_repairs_sparse_index_membership() -> None:
    plan = {
        cleanup.SPEECH_CLEANUP_INTERNAL_FIELD: {
            cleanup.SOURCE_COPY_CLEANUP_FIELD: [],
            "unrelated_private_state": {"keep": True},
        }
    }

    assert cleanup.prune_empty_cleanup_keys(plan)
    assert plan == {
        cleanup.SPEECH_CLEANUP_INTERNAL_FIELD: {"unrelated_private_state": {"keep": True}}
    }


def test_retained_receipt_rotates_behind_later_work_with_bounded_metadata() -> None:
    job = _job()
    lease = datetime.now(UTC)
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    for attempt_id in (first_id, second_id):
        cleanup.reserve_source_copy_cleanup(
            job.assembly_plan,
            job_id=job.id,
            copy_attempt_id=attempt_id,
            lease_expires_at=lease,
        )

    cleanup.rotate_retained_cleanup_receipt(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
            receipt_id=str(first_id),
        ),
        status="unavailable",
        attempted_at=lease,
        error_class="google.api transport failure with private detail!",
    )

    receipts = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.SOURCE_COPY_CLEANUP_FIELD
    ]
    assert [receipt["copy_attempt_id"] for receipt in receipts] == [
        str(second_id),
        str(first_id),
    ]
    assert receipts[-1]["failure_count"] == 1
    assert receipts[-1]["last_status"] == "unavailable"
    assert len(receipts[-1]["last_error_class"]) <= cleanup.FAILURE_CLASS_MAX_LENGTH
    assert " " not in receipts[-1]["last_error_class"]


def test_malformed_failure_counter_does_not_pop_durable_receipt() -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=datetime.now(UTC),
    )
    receipts = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.SOURCE_COPY_CLEANUP_FIELD
    ]
    receipts[0]["failure_count"] = {"malformed": True}
    snapshot = copy.deepcopy(job.assembly_plan)

    with pytest.raises(cleanup.CleanupReceiptError, match="failure_count"):
        cleanup.rotate_retained_cleanup_receipt(
            job.assembly_plan,
            cleanup.CleanupReceiptLocator(
                field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
                receipt_id=str(attempt_id),
            ),
            status="unavailable",
            attempted_at=datetime.now(UTC),
        )

    assert job.assembly_plan == snapshot


def test_reference_proof_excludes_only_canonical_exact_debt_receipt() -> None:
    job = _job()
    old_path = f"generative-jobs/{job.id}/legacy.mp4"
    live_path = f"generative-jobs/{job.id}/live.mp4"
    debt_id = uuid.uuid4()
    receipt = cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=debt_id,
        paths=[old_path],
    )
    job.assembly_plan["variants"] = [{"video_path": live_path}]

    proof = cleanup.prove_job_storage_references(
        job,
        exclude_receipt=cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=receipt["debt_id"],
        ),
    )

    assert proof.proved
    assert proof.references == frozenset({live_path})
    assert old_path not in proof.references


def test_duplicate_exact_debt_remains_reference_bearing_until_coalesced() -> None:
    job = _job()
    old_path = f"generative-jobs/{job.id}/legacy.mp4"
    first = cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[old_path],
    )
    second = cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[old_path],
    )

    before = cleanup.prove_job_storage_references(
        job,
        exclude_receipt=cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=first["debt_id"],
        ),
    )
    assert old_path in before.references

    assert cleanup.coalesce_exact_key_cleanup(job.assembly_plan, job=job)
    receipts = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ]
    assert receipts == [
        {
            "debt_id": min(first["debt_id"], second["debt_id"]),
            "kind": "exact_keys",
            "paths": [old_path],
            "upload_state": "closed",
        }
    ]


def test_prefix_cleanup_waits_for_writing_lease_then_becomes_deletable() -> None:
    job = _job()
    now = datetime.now(UTC)
    generation = uuid.uuid4()
    raw = cleanup.reserve_render_generation_cleanup(
        job.assembly_plan,
        job_id=job.id,
        generation=generation,
        lease_expires_at=now + timedelta(minutes=10),
    )
    receipt = cleanup.parse_cleanup_receipt(
        job=job,
        field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
        raw=raw,
    )
    assert isinstance(receipt, cleanup.PrefixCleanupReceipt)
    proof = cleanup.StorageReferenceProof(status="proved")

    assert cleanup.classify_prefix_cleanup(receipt, proof, now=now).disposition == (
        "wait_for_uploads"
    )
    assert (
        cleanup.classify_prefix_cleanup(
            receipt,
            proof,
            now=now + timedelta(minutes=11),
        ).disposition
        == "delete"
    )


def test_live_reference_blocks_prefix_cleanup() -> None:
    job = _job()
    generation = uuid.uuid4()
    raw = cleanup.new_render_generation_receipt(
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )
    raw["upload_state"] = "closed"
    receipt = cleanup.parse_cleanup_receipt(
        job=job,
        field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
        raw=raw,
    )
    assert isinstance(receipt, cleanup.PrefixCleanupReceipt)
    live_path = f"{receipt.prefix}variant.mp4"
    proof = cleanup.StorageReferenceProof(status="proved", references=frozenset({live_path}))

    decision = cleanup.classify_prefix_cleanup(receipt, proof, now=datetime.now(UTC))

    assert decision.disposition == "referenced"
    assert decision.references == (live_path,)


def test_cleanup_selector_matches_sparse_partial_index_literal() -> None:
    statements = []

    class _Result:
        def scalars(self):
            return []

    class _CaptureSession:
        def execute(self, statement):
            statements.append(statement)
            return _Result()

    assert cleanup.jobs_with_storage_attempt_cleanup_receipts(_CaptureSession(), limit=5) == []
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "jsonb_typeof(jobs.assembly_plan -> '_speech_cleanup_internal') = 'object'" in sql
    assert "? 'durable_source_copy_pending'" in sql
    assert "? 'render_generation_cleanup_pending'" in sql
    assert "ORDER BY jobs.updated_at ASC, jobs.id ASC" in sql


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_source_reconcile_deletes_orphan_only_after_verified_empty(monkeypatch) -> None:
    job = _job()
    job.all_candidates = {"clip_paths": ["dev-user/original.mp4"]}
    attempt_id = uuid.uuid4()
    raw = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=datetime.now(UTC),
    )
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
            receipt_id=str(attempt_id),
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda prefix, *, timeout_s: (
            calls.append(prefix)
            or storage.PrefixDeletionResult(status="verified_empty", remaining=0)
        ),
    )
    session = _Session()

    result = cleanup.reconcile_durable_source_copy_cleanup_locked(session, job)

    assert result.deleted == 1
    assert result.retained == 0
    assert calls == [raw["prefix"]]
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan
    assert session.commits == 1


def test_source_reconcile_consumes_adopted_live_receipt_without_delete(monkeypatch) -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    raw = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=datetime.now(UTC),
    )
    live_path = f"{raw['prefix']}{uuid.uuid4()}/clip.mp4"
    job.all_candidates = {"clip_paths": [live_path]}
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
            receipt_id=str(attempt_id),
        ),
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda *_args, **_kwargs: pytest.fail("live source prefix must not be deleted"),
    )

    result = cleanup.reconcile_durable_source_copy_cleanup_locked(_Session(), job)

    assert result.adopted_live == 1
    assert result.deleted == 0
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan


def test_expired_writing_source_receipt_can_be_adopted_after_hard_kill(monkeypatch) -> None:
    job = _job()
    attempt_id = uuid.uuid4()
    raw = cleanup.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    job.all_candidates = {"clip_paths": [f"{raw['prefix']}{uuid.uuid4()}/clip.mp4"]}
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda *_args, **_kwargs: pytest.fail("adopted source prefix must not be deleted"),
    )

    result = cleanup.reconcile_durable_source_copy_cleanup_locked(_Session(), job)

    assert result.adopted_live == 1
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan


def test_source_reconcile_retains_ambiguous_prefix_and_rotates(monkeypatch) -> None:
    job = _job()
    job.all_candidates = {"clip_paths": ["dev-user/original.mp4"]}
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    for attempt_id in (first_id, second_id):
        cleanup.reserve_source_copy_cleanup(
            job.assembly_plan,
            job_id=job.id,
            copy_attempt_id=attempt_id,
            lease_expires_at=datetime.now(UTC),
        )
        cleanup.mark_cleanup_receipt_closed(
            job.assembly_plan,
            cleanup.CleanupReceiptLocator(
                field=cleanup.SOURCE_COPY_CLEANUP_FIELD,
                receipt_id=str(attempt_id),
            ),
        )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda _prefix, *, timeout_s: storage.PrefixDeletionResult(
            status="unavailable", remaining=None
        ),
    )

    result = cleanup.reconcile_durable_source_copy_cleanup_locked(
        _Session(),
        job,
        limit=1,
    )

    receipts = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.SOURCE_COPY_CLEANUP_FIELD
    ]
    assert result.failures == 1
    assert result.retained == 2
    assert [receipt["copy_attempt_id"] for receipt in receipts] == [
        str(second_id),
        str(first_id),
    ]
    assert receipts[-1]["last_status"] == "unavailable"


def test_render_reconcile_deletes_closed_unreferenced_generation(monkeypatch) -> None:
    job = _job()
    generation = uuid.uuid4()
    raw = cleanup.reserve_render_generation_cleanup(
        job.assembly_plan,
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=str(generation),
        ),
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda prefix, *, timeout_s: (
            calls.append(prefix)
            or storage.PrefixDeletionResult(status="verified_empty", remaining=0)
        ),
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.deleted == 1
    assert result.retained == 0
    assert calls == [raw["prefix"]]
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan


def test_render_reconcile_consumes_exact_closed_live_generation(monkeypatch) -> None:
    job = _job()
    generation = uuid.uuid4()
    raw = cleanup.reserve_render_generation_cleanup(
        job.assembly_plan,
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=str(generation),
        ),
    )
    live_path = f"{raw['prefix']}variant.mp4"
    job.assembly_plan["variants"] = [
        {
            "variant_id": "subtitled",
            "render_generation_id": str(generation),
            "render_status": "ready",
            "ok": True,
            "video_path": live_path,
        }
    ]
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda *_args, **_kwargs: pytest.fail("referenced generation must not be deleted"),
    )
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path == live_path,
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.deleted == 0
    assert result.adopted_live == 1
    assert result.retained == 0
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan


def test_render_reconcile_retains_live_generation_when_object_is_missing(monkeypatch) -> None:
    job = _job()
    generation = uuid.uuid4()
    raw = cleanup.reserve_render_generation_cleanup(
        job.assembly_plan,
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=str(generation),
        ),
    )
    job.assembly_plan["variants"] = [
        {
            "variant_id": "subtitled",
            "render_generation_id": str(generation),
            "render_status": "ready",
            "ok": True,
            "video_path": f"{raw['prefix']}variant.mp4",
        }
    ]
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.object_exists_once", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda *_args, **_kwargs: pytest.fail("unproved live generation must not be deleted"),
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.failures == 1
    assert result.adopted_live == 0
    assert result.retained == 1
    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["last_status"] == "live_object_unavailable"


def test_render_reconcile_does_not_consume_live_generation_with_private_owner(
    monkeypatch,
) -> None:
    job = _job()
    generation = uuid.uuid4()
    raw = cleanup.reserve_render_generation_cleanup(
        job.assembly_plan,
        job_id=job.id,
        generation=generation,
        lease_expires_at=datetime.now(UTC),
    )
    cleanup.mark_cleanup_receipt_closed(
        job.assembly_plan,
        cleanup.CleanupReceiptLocator(
            field=cleanup.RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=str(generation),
        ),
    )
    job.assembly_plan["variants"] = [
        {
            "variant_id": "subtitled",
            "render_generation_id": str(generation),
            "render_status": "ready",
            "ok": True,
            "video_path": f"{raw['prefix']}variant.mp4",
        }
    ]
    job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD]["required_speech_generation_locks"] = {
        "subtitled": str(generation)
    }
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda *_args, **_kwargs: pytest.fail("private owner must block live consumption"),
    )
    monkeypatch.setattr(
        "app.storage.delete_prefix_verified",
        lambda *_args, **_kwargs: pytest.fail("referenced generation must not be deleted"),
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.adopted_live == 0
    assert result.retained == 1
    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["last_status"] == "referenced"


def test_render_exact_debt_keeps_only_unavailable_paths(monkeypatch) -> None:
    job = _job()
    first = f"generative-jobs/{job.id}/old-first.mp4"
    second = f"generative-jobs/{job.id}/old-second.mp4"
    cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[first, second],
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path == second,
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.failures == 1
    assert result.retained == 1
    assert deleted == [first, second]
    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["paths"] == [second]
    assert receipt["last_status"] == "partial"


def test_render_exact_debt_self_exclusion_makes_progress(monkeypatch) -> None:
    job = _job()
    old_path = f"generative-jobs/{job.id}/old.mp4"
    cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[old_path],
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.delete_object_once", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.storage.object_exists_once", lambda *_args, **_kwargs: False)

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert result.deleted == 1
    assert result.retained == 0
    assert cleanup.SPEECH_CLEANUP_INTERNAL_FIELD not in job.assembly_plan


def test_render_exact_debt_processes_only_bounded_path_page(monkeypatch) -> None:
    job = _job()
    paths = [f"generative-jobs/{job.id}/old-{index}.mp4" for index in range(4)]
    cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=paths,
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )
    monkeypatch.setattr("app.storage.object_exists_once", lambda *_args, **_kwargs: False)

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job, limit=8)

    assert deleted == paths[: cleanup.CLEANUP_EXACT_PATH_CAP_PER_PASS]
    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["paths"] == paths[cleanup.CLEANUP_EXACT_PATH_CAP_PER_PASS :]
    assert result.receipts_seen == 1
    assert result.retained == 1
    assert result.failures == 1


def test_render_exact_debt_deadline_retains_unverified_deleted_path(monkeypatch) -> None:
    job = _job()
    path = f"generative-jobs/{job.id}/old.mp4"
    cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[path],
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    clock = iter([0.0, 1.0, cleanup.CLEANUP_STORAGE_TOTAL_BUDGET_S + 1.0])
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: next(clock))
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda value, *, timeout_s: deleted.append(value) or True,
    )
    head_calls: list[str] = []
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda value, *, timeout_s: head_calls.append(value) or False,
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    assert deleted == [path]
    assert head_calls == []
    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["paths"] == [path]
    assert receipt["last_status"] == "partial"
    assert result.retained == 1


def test_render_exact_debt_revalidates_references_after_storage_io(monkeypatch) -> None:
    job = _job()
    path = f"generative-jobs/{job.id}/old.mp4"
    cleanup.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[path],
    )
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.delete_object_once", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.storage.object_exists_once", lambda *_args, **_kwargs: False)
    proofs = iter(
        [
            cleanup.StorageReferenceProof(status="proved"),
            cleanup.StorageReferenceProof(status="proved", references=frozenset({path})),
        ]
    )
    monkeypatch.setattr(
        cleanup,
        "prove_job_storage_references",
        lambda *_args, **_kwargs: next(proofs),
    )

    result = cleanup.reconcile_render_generation_cleanup_locked(_Session(), job)

    receipt = job.assembly_plan[cleanup.SPEECH_CLEANUP_INTERNAL_FIELD][
        cleanup.RENDER_GENERATION_CLEANUP_FIELD
    ][0]
    assert receipt["paths"] == [path]
    assert receipt["last_status"] == "reference_changed"
    assert result.deleted == 0
    assert result.retained == 1
