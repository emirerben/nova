from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import storage
from app.services import durable_attempt_cleanup as attempts
from app.services import job_storage_deletion as manifests


def test_legacy_exact_path_manifest_round_trips_unchanged() -> None:
    job_id = uuid.uuid4()
    payload = [f"jobs/{job_id}/a.mp4", f"jobs/{job_id}/a.mp4"]

    manifest = manifests.parse_job_storage_manifest(payload, job_id=job_id)

    assert manifest.legacy
    assert manifest.exact_paths == (f"jobs/{job_id}/a.mp4",)
    assert manifest.to_payload() == [f"jobs/{job_id}/a.mp4"]


@pytest.mark.parametrize("template", manifests.JOB_OUTPUT_PREFIXES)
def test_legacy_manifest_rejects_another_jobs_exact_output_without_deleting(
    monkeypatch, template: str
) -> None:
    job_id = uuid.uuid4()
    foreign_path = f"{template.format(job_id=uuid.uuid4())}output.mp4"
    deleted: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    with pytest.raises(manifests.JobStorageManifestError, match="another Job"):
        manifest = manifests.parse_job_storage_manifest([foreign_path], job_id=job_id)
        manifests.cleanup_job_storage_manifest(manifest, now=datetime.now(UTC))

    assert deleted == []


@pytest.mark.parametrize("template", manifests.JOB_OUTPUT_PREFIXES)
def test_v2_manifest_rejects_another_jobs_exact_output_without_deleting(
    monkeypatch, template: str
) -> None:
    job_id = uuid.uuid4()
    foreign_path = f"{template.format(job_id=uuid.uuid4())}output.mp4"
    deleted: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )
    payload = {"version": 2, "exact_paths": [foreign_path], "prefixes": []}

    with pytest.raises(manifests.JobStorageManifestError, match="another Job"):
        manifest = manifests.parse_job_storage_manifest(payload, job_id=job_id)
        manifests.cleanup_job_storage_manifest(manifest, now=datetime.now(UTC))

    assert deleted == []


@pytest.mark.parametrize("legacy", [True, False])
def test_exact_manifest_retains_supported_shared_input_namespaces(legacy: bool) -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    exact_paths = [
        f"dev-user/{user_id}/generative/upload-token/clip.mp4",
        f"voiceover-uploads/direct/{user_id}/voice-token/voice.webm",
    ]
    payload: object = (
        exact_paths if legacy else {"version": 2, "exact_paths": exact_paths, "prefixes": []}
    )

    manifest = manifests.parse_job_storage_manifest(payload, job_id=job_id)

    assert manifest.exact_paths == tuple(exact_paths)


def test_v2_manifest_rejects_prefix_outside_job_allowlist() -> None:
    job_id = uuid.uuid4()
    payload = {
        "version": 2,
        "exact_paths": [],
        "prefixes": [
            {
                "prefix": f"generative-jobs/{uuid.uuid4()}/",
                "not_before": datetime.now(UTC).isoformat(),
            }
        ],
    }

    with pytest.raises(manifests.JobStorageManifestError, match="allowlist"):
        manifests.parse_job_storage_manifest(payload, job_id=job_id)


def test_v2_merge_deduplicates_paths_and_keeps_latest_prefix_deadline() -> None:
    job_id = uuid.uuid4()
    prefix = f"generative-jobs/{job_id}/"
    early = datetime.now(UTC)
    late = early + timedelta(hours=1)
    existing = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[f"jobs/{job_id}/a.mp4"],
        prefixes=[prefix],
        not_before=early,
    )
    incoming = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[f"jobs/{job_id}/a.mp4", f"jobs/{job_id}/b.mp4"],
        prefixes=[prefix],
        not_before=late,
    )

    merged = manifests.merge_job_storage_manifests(
        existing.to_payload(),
        incoming,
        job_id=job_id,
    )

    assert merged.exact_paths == (
        f"jobs/{job_id}/a.mp4",
        f"jobs/{job_id}/b.mp4",
    )
    assert merged.prefixes == (manifests.JobStoragePrefix(prefix=prefix, not_before=late),)


def test_parent_prefix_deadline_is_promoted_to_latest_child_lease() -> None:
    job_id = uuid.uuid4()
    parent = f"generative-jobs/{job_id}/"
    child = f"{parent}render-generations/{uuid.uuid4()}/"
    early = datetime.now(UTC)
    late = early + timedelta(hours=1)
    payload = {
        "version": 2,
        "exact_paths": [],
        "prefixes": [
            {"prefix": parent, "not_before": early.isoformat()},
            {"prefix": child, "not_before": late.isoformat()},
        ],
    }

    manifest = manifests.parse_job_storage_manifest(payload, job_id=job_id)

    by_prefix = {entry.prefix: entry.not_before for entry in manifest.prefixes}
    assert by_prefix[parent] == late
    assert by_prefix[child] == late


def test_manifest_for_job_externalizes_private_receipts_and_advances_root_lease() -> None:
    now = datetime.now(UTC)
    long_lease = now + timedelta(hours=2)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        raw_storage_path="dev-user/raw.mp4",
        all_candidates={},
        probe_metadata=None,
        transcript=None,
        scene_cuts=None,
        assembly_plan={},
    )
    attempt_id = uuid.uuid4()
    receipt = attempts.reserve_source_copy_cleanup(
        job.assembly_plan,
        job_id=job.id,
        copy_attempt_id=attempt_id,
        lease_expires_at=long_lease,
    )
    exact_path = f"generative-jobs/{job.id}/legacy.mp4"
    attempts.append_exact_key_cleanup(
        job.assembly_plan,
        job=job,
        debt_id=uuid.uuid4(),
        paths=[exact_path],
    )

    manifest = manifests.build_job_storage_manifest_for_job(
        job=job,
        exact_paths=[f"jobs/{job.id}/output.mp4"],
        conservative_not_before=now + timedelta(minutes=30),
    )

    assert set(manifest.exact_paths) == {
        f"jobs/{job.id}/output.mp4",
        exact_path,
    }
    by_prefix = {entry.prefix: entry.not_before for entry in manifest.prefixes}
    assert by_prefix[receipt["prefix"]] == long_lease
    assert by_prefix[f"generative-jobs/{job.id}/"] == long_lease
    assert by_prefix[f"{job.user_id}/{job.id}/"] == now + timedelta(minutes=30)
    assert set(manifests.conservative_job_prefixes(job.id)) <= set(by_prefix)


def test_cleanup_retains_not_before_prefix_without_touching_storage(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[],
        prefixes=[f"generative-jobs/{job_id}/"],
        not_before=now + timedelta(minutes=1),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda prefix, *, timeout_s: calls.append(prefix),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.status == "pending"
    assert result.prefixes_retained == 1
    assert calls == []


@pytest.mark.parametrize("prefix_status", ["partial", "unavailable"])
def test_cleanup_retains_prefix_until_verified_empty(monkeypatch, prefix_status: str) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    prefix = f"generative-jobs/{job_id}/"
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[],
        prefixes=[prefix],
        not_before=now,
    )
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda _prefix, *, timeout_s: storage.PrefixDeletionResult(
            status=prefix_status,
            listed=1,
            deleted=0,
            failed=1,
            remaining=None if prefix_status == "unavailable" else 1,
        ),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.status == ("unavailable" if prefix_status == "unavailable" else "pending")
    assert result.prefixes_retained == 1
    assert result.remaining.prefixes[0].prefix == prefix


def test_cleanup_completes_only_after_exact_and_prefix_cleanup(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    exact = f"jobs/{job_id}/output.mp4"
    prefix = f"generative-jobs/{job_id}/"
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[exact],
        prefixes=[prefix],
        not_before=now,
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )
    monkeypatch.setattr(storage, "object_exists_once", lambda _path, *, timeout_s: False)
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda _prefix, *, timeout_s: storage.PrefixDeletionResult(
            status="verified_empty",
            listed=0,
            deleted=0,
            failed=0,
            remaining=0,
        ),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.complete
    assert result.exact_deleted == 1
    assert result.prefixes_verified == 1
    assert deleted == [exact]
    assert result.remaining.to_payload() == {
        "version": 2,
        "exact_paths": [],
        "prefixes": [],
    }


def test_v2_exact_key_remains_pending_when_absence_cannot_be_verified(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    exact = f"jobs/{job_id}/output.mp4"
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[exact],
        prefixes=[],
        not_before=now,
    )
    monkeypatch.setattr(storage, "delete_object_once", lambda _path, *, timeout_s: True)
    monkeypatch.setattr(
        storage,
        "object_exists_once",
        lambda _path, *, timeout_s: (_ for _ in ()).throw(RuntimeError("HEAD unavailable")),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.status == "unavailable"
    assert result.exact_failed == 1
    assert result.remaining.exact_paths == (exact,)


def test_cleanup_attempt_limits_persist_unattempted_targets(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    exact_paths = [f"jobs/{job_id}/output-{index}.mp4" for index in range(2)]
    prefixes = [
        f"generative-jobs/{job_id}/",
        f"job-posters/{job_id}/",
    ]
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=exact_paths,
        prefixes=prefixes,
        not_before=now,
    )
    deleted_exact: list[str] = []
    deleted_prefixes: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_object_once",
        lambda path, *, timeout_s: deleted_exact.append(path) or True,
    )
    monkeypatch.setattr(storage, "object_exists_once", lambda _path, *, timeout_s: False)
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda prefix, *, timeout_s: (
            deleted_prefixes.append(prefix)
            or storage.PrefixDeletionResult(status="verified_empty", remaining=0)
        ),
    )

    result = manifests.cleanup_job_storage_manifest(
        manifest,
        now=now,
        prefix_limit=1,
        exact_limit=1,
    )

    assert result.status == "pending"
    assert deleted_exact == exact_paths[:1]
    assert deleted_prefixes == [prefixes[0]]
    assert result.remaining.exact_paths == (exact_paths[1],)
    assert result.remaining.prefixes == (
        manifests.JobStoragePrefix(prefix=prefixes[1], not_before=now),
    )


def test_verified_parent_avoids_relisting_each_child_prefix(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    parent = f"generative-jobs/{job_id}/"
    child = f"{parent}render-generations/{uuid.uuid4()}/"
    payload = {
        "version": 2,
        "exact_paths": [],
        "prefixes": [
            {"prefix": parent, "not_before": now.isoformat()},
            {"prefix": child, "not_before": now.isoformat()},
        ],
    }
    manifest = manifests.parse_job_storage_manifest(payload, job_id=job_id)
    calls: list[str] = []
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda prefix, *, timeout_s: (
            calls.append(prefix)
            or storage.PrefixDeletionResult(status="verified_empty", remaining=0)
        ),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.complete
    assert result.prefixes_verified == 2
    assert calls == [parent]


def test_verified_parent_is_also_absence_proof_for_exact_child_key(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    parent = f"generative-jobs/{job_id}/"
    exact = f"{parent}render-generations/{uuid.uuid4()}/output.mp4"
    manifest = manifests.build_job_storage_manifest(
        job_id=job_id,
        exact_paths=[exact],
        prefixes=[parent],
        not_before=now,
    )
    monkeypatch.setattr(
        storage,
        "delete_prefix_verified",
        lambda _prefix, *, timeout_s: storage.PrefixDeletionResult(
            status="verified_empty", remaining=0
        ),
    )
    monkeypatch.setattr(
        storage,
        "delete_object_once",
        lambda *_args, **_kwargs: pytest.fail("verified parent already proved absence"),
    )

    result = manifests.cleanup_job_storage_manifest(manifest, now=now)

    assert result.complete
    assert result.exact_deleted == 1


def test_manifest_for_job_fails_closed_on_malformed_private_receipt() -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan={
            attempts.SPEECH_CLEANUP_INTERNAL_FIELD: {
                attempts.SOURCE_COPY_CLEANUP_FIELD: [{"copy_attempt_id": "broken"}]
            }
        },
    )

    with pytest.raises(manifests.JobStorageManifestError, match="malformed"):
        manifests.build_job_storage_manifest_for_job(
            job=job,
            exact_paths=[],
            conservative_not_before=datetime.now(UTC),
        )


def test_account_erasure_falls_back_to_conservative_roots_on_malformed_receipt() -> None:
    now = datetime.now(UTC)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan={
            attempts.SPEECH_CLEANUP_INTERNAL_FIELD: {
                attempts.SOURCE_COPY_CLEANUP_FIELD: [{"copy_attempt_id": "broken"}]
            }
        },
    )
    exact = f"jobs/{job.id}/known.mp4"

    manifest = manifests.build_account_erasure_manifest_for_job(
        job=job,
        exact_paths=[exact],
        conservative_not_before=now,
    )

    assert manifest.exact_paths == (exact,)
    assert {entry.prefix for entry in manifest.prefixes} == set(
        manifests.conservative_job_prefixes(job.id, user_id=job.user_id)
    )
    assert all(entry.not_before == now for entry in manifest.prefixes)
