from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.dialects import postgresql

import app.services.video_poster_cleanup as cleanup


class _EmptyScalars:
    def scalars(self):
        return self

    def all(self):
        return []


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    def execute(self, *_args, **_kwargs):
        return _EmptyScalars()

    def commit(self) -> None:
        self.commits += 1


def _job(plan: dict) -> SimpleNamespace:
    job_id = uuid.uuid4()
    return SimpleNamespace(
        id=job_id,
        user_id=uuid.uuid4(),
        assembly_plan=plan,
    )


def _uuid_poster(source: str) -> str:
    return f"{source}{cleanup.VIDEO_POSTER_BACKFILL_MARKER}{uuid.uuid4()}.jpg"


def test_cleanup_selector_literalizes_receipt_key_for_partial_index() -> None:
    statements = []

    class _Result:
        def scalars(self):
            return []

    class _CaptureSession:
        def execute(self, statement):
            statements.append(statement)
            return _Result()

    assert cleanup.jobs_with_video_poster_cleanup_receipts(_CaptureSession(), limit=10) == []

    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "jsonb_typeof(jobs.assembly_plan) = 'object'" in sql
    assert "assembly_plan ? '_poster_backfill_cleanup_receipts'" in sql
    assert "assembly_plan_1" not in sql
    assert "ORDER BY jobs.updated_at ASC, jobs.id ASC" in sql


def test_present_empty_receipt_list_self_heals(monkeypatch) -> None:
    job = _job({cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: []})
    session = _Session()
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert result.ok
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan
    assert session.commits == 1


@pytest.mark.parametrize("raw_receipt", [{"not": "a receipt"}, None, "malformed"])
def test_malformed_receipt_field_remains_durable_debt(monkeypatch, raw_receipt) -> None:
    job = _job({cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: raw_receipt})
    session = _Session()
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert not result.ok
    assert result.receipts_seen == 1
    assert result.failures == 1
    assert result.retained == 1
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [raw_receipt]
    assert session.commits == 1


def test_append_preserves_malformed_receipt_with_null_replacement() -> None:
    malformed_old = _uuid_poster(f"generative-jobs/{uuid.uuid4()}/malformed.mp4")
    malformed = {
        "old_path": malformed_old,
        "replacement_path": None,
        "diagnostic": {"preserve": True},
    }
    old_path = _uuid_poster(f"generative-jobs/{uuid.uuid4()}/old.mp4")
    replacement = f"generative-jobs/{uuid.uuid4()}/current.mp4"
    plan = {cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [malformed]}

    changed = cleanup.append_video_poster_cleanup_receipt(
        plan,
        old_path=old_path,
        replacement_path=replacement,
    )

    assert changed
    assert plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        malformed,
        {"old_path": old_path, "replacement_path": replacement},
    ]


def test_append_normalizes_legacy_single_mapping_without_data_loss() -> None:
    legacy = {
        "old_path": _uuid_poster(f"jobs/{uuid.uuid4()}/legacy.mp4"),
        "replacement_path": None,
        "diagnostic": {"preserve": [1, 2, 3]},
    }
    legacy_snapshot = copy.deepcopy(legacy)
    old_path = _uuid_poster(f"jobs/{uuid.uuid4()}/old.mp4")
    replacement = f"jobs/{uuid.uuid4()}/current.mp4"
    plan = {cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: legacy}

    changed = cleanup.append_video_poster_cleanup_receipt(
        plan,
        old_path=old_path,
        replacement_path=replacement,
    )

    assert changed
    assert plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        legacy_snapshot,
        {"old_path": old_path, "replacement_path": replacement},
    ]
    assert plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD][0] is not legacy


def test_uuid_receipt_with_null_replacement_is_retained_and_fails_closed(
    monkeypatch,
) -> None:
    job = _job({})
    malformed = {
        "old_path": _uuid_poster(f"jobs/{job.id}/old.mp4"),
        "replacement_path": None,
        "diagnostic": "keep-me",
    }
    job.assembly_plan = {cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [malformed]}
    session = _Session()
    storage_calls: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: storage_calls.append(path) or True,
    )

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert not result.ok
    assert result.failures == 1
    assert result.retained == 1
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [malformed]
    assert storage_calls == []
    assert session.commits == 1


def test_absent_receipt_field_is_a_true_noop(monkeypatch) -> None:
    job = _job({})
    session = _Session()
    modified: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: modified.append("modified"))

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert result == cleanup.VideoPosterCleanupResult()
    assert session.commits == 0
    assert modified == []


def test_delete_failure_chain_retargets_posterless_video_swap_for_later_retry(
    monkeypatch,
) -> None:
    job = _job({})
    replacement_video = f"generative-jobs/{job.id}/current.mp4"
    old_poster = _uuid_poster(f"generative-jobs/{job.id}/old.mp4")
    job.assembly_plan = {
        "variants": [{"variant_id": "v1", "video_path": replacement_video}],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": old_poster, "replacement_path": replacement_video}
        ],
    }
    session = _Session()
    delete_attempts = 0
    deleted: list[str] = []
    existing_objects = {old_poster, replacement_video}

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path in existing_objects,
    )

    def _delete(path: str, *, timeout_s: float) -> bool:
        nonlocal delete_attempts
        deleted.append(path)
        delete_attempts += 1
        if delete_attempts == 1:
            raise RuntimeError("delete unavailable")
        existing_objects.discard(path)
        return True

    monkeypatch.setattr("app.storage.delete_object_once", _delete)

    first = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert first.failures == 1
    assert first.retained == 1
    assert not first.ok
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": replacement_video}
    ]

    next_video = f"generative-jobs/{job.id}/next.mp4"
    journaled = cleanup.append_retired_variant_poster_receipts(
        job.assembly_plan,
        {"poster_path": None, "video_path": replacement_video},
        {"poster_path": None, "video_path": next_video},
    )
    job.assembly_plan["variants"][0]["video_path"] = next_video
    existing_objects.add(next_video)

    assert journaled == [replacement_video]
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": next_video}
    ]

    second = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert second.deleted == 1
    assert second.retained == 0
    assert second.ok
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan
    assert deleted == [old_poster, old_poster]
    assert session.commits == 2


def test_unreferenced_replacement_is_pending_failure(monkeypatch) -> None:
    job = _job({})
    current_video = f"generative-jobs/{job.id}/current.mp4"
    missing_reference = f"generative-jobs/{job.id}/not-current.mp4"
    old_poster = _uuid_poster(f"generative-jobs/{job.id}/old.mp4")
    job.assembly_plan = {
        "variants": [{"variant_id": "v1", "video_path": current_video}],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": old_poster, "replacement_path": missing_reference}
        ],
    }
    session = _Session()
    deleted: list[str] = []

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path == old_poster,
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert not result.ok
    assert result.failures == 1
    assert result.retained == 1
    assert deleted == []


@pytest.mark.parametrize("failing_head", ["old", "replacement"])
def test_storage_head_exception_retains_receipt_and_deletes_nothing(
    monkeypatch,
    failing_head: str,
) -> None:
    job = _job({})
    replacement = f"generative-jobs/{job.id}/current.mp4"
    old_poster = _uuid_poster(f"generative-jobs/{job.id}/old.mp4")
    receipt = {"old_path": old_poster, "replacement_path": replacement}
    job.assembly_plan = {
        "variants": [{"variant_id": "v1", "video_path": replacement}],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [receipt],
    }
    session = _Session()
    deleted: list[str] = []

    def _object_exists(path: str, *, timeout_s: float) -> bool:
        if (failing_head == "old" and path == old_poster) or (
            failing_head == "replacement" and path == replacement
        ):
            raise RuntimeError("storage HEAD unavailable")
        return True

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.object_exists_once", _object_exists)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert not result.ok
    assert result.failures == 1
    assert result.retained == 1
    assert result.deleted == 0
    assert deleted == []
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [receipt]
    assert session.commits == 1


def test_already_absent_old_object_completes_stale_receipt(monkeypatch) -> None:
    job = _job({})
    current_video = f"music-jobs/{job.id}/current.mp4"
    superseded_replacement = f"music-jobs/{job.id}/previous.mp4"
    old_poster = _uuid_poster(f"music-jobs/{job.id}/old.mp4")
    job.assembly_plan = {
        "output_path": current_video,
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": old_poster, "replacement_path": superseded_replacement}
        ],
    }
    deleted: list[str] = []

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr("app.storage.object_exists_once", lambda _path, *, timeout_s: False)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(_Session(), job)

    assert result.ok
    assert result.ignored == 1
    assert result.retained == 0
    assert deleted == []
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan


def test_top_level_receipt_chain_retargets_across_posterless_rerender() -> None:
    job_id = uuid.uuid4()
    old_video = f"jobs/{job_id}/old.mp4"
    old_poster = _uuid_poster(old_video)
    current_video = f"jobs/{job_id}/current.mp4"
    current_poster = f"{current_video}.poster.jpg"
    previous = {
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": old_poster, "replacement_path": current_poster}
        ],
        "output_path": current_video,
        "poster_path": current_poster,
    }
    next_video = f"jobs/{job_id}/next.mp4"
    current = {"output_path": next_video, "poster_path": None}
    plan = dict(current)

    journaled = cleanup.append_retired_top_level_poster_receipts(
        plan,
        previous,
        current,
    )

    assert journaled == [current_poster]
    assert plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": next_video}
    ]


def test_top_level_helper_journals_primary_and_base_uuid_posters() -> None:
    job_id = uuid.uuid4()
    old_output = f"jobs/{job_id}/old-output.mp4"
    old_base = f"jobs/{job_id}/old-base.mp4"
    old_poster = _uuid_poster(old_output)
    old_base_poster = _uuid_poster(old_base)
    previous = {
        "output_path": old_output,
        "poster_path": old_poster,
        "base_output_url": old_base,
        "base_poster_path": old_base_poster,
    }
    next_output = f"jobs/{job_id}/next-output.mp4"
    next_base = f"jobs/{job_id}/next-base.mp4"
    current = {
        "output_path": next_output,
        "poster_path": f"{next_output}.poster.jpg",
        "base_output_url": next_base,
        "base_poster_path": None,
    }
    plan = dict(current)

    journaled = cleanup.append_retired_top_level_poster_receipts(
        plan,
        previous,
        current,
    )

    assert journaled == [old_poster, old_base_poster]
    assert plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {
            "old_path": old_poster,
            "replacement_path": current["poster_path"],
        },
        {"old_path": old_base_poster, "replacement_path": next_base},
    ]


def test_rollback_snapshot_keeps_old_poster_until_publish_clears_it(monkeypatch) -> None:
    job = _job({})
    current_video = f"generative-jobs/{job.id}/current.mp4"
    current_poster = f"{current_video}.poster.jpg"
    old_video = f"generative-jobs/{job.id}/old.mp4"
    old_poster = _uuid_poster(old_video)
    old_variant = {
        "variant_id": "v1",
        "video_path": old_video,
        "poster_path": old_poster,
    }
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "v1",
                "video_path": current_video,
                "poster_path": current_poster,
            }
        ],
        "speech_cut_previous_variant": old_variant,
        "speech_cut_previous_variants": [old_variant],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": old_poster, "replacement_path": current_poster}
        ],
    }
    session = _Session()
    deleted: list[str] = []

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda path, *, timeout_s: path in {old_poster, current_poster},
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    while_rollback_is_live = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert while_rollback_is_live.retained == 1
    assert while_rollback_is_live.failures == 0
    assert deleted == []

    job.assembly_plan["speech_cut_previous_variant"] = None
    job.assembly_plan["speech_cut_previous_variants"] = None
    after_publish = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert after_publish.deleted == 1
    assert deleted == [old_poster]
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan


def test_deterministic_old_poster_is_never_deleted(monkeypatch) -> None:
    job = _job({})
    current_video = f"generative-jobs/{job.id}/current.mp4"
    deterministic_old = f"generative-jobs/{job.id}/old.mp4.poster.jpg"
    job.assembly_plan = {
        "variants": [{"variant_id": "v1", "video_path": current_video}],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [
            {"old_path": deterministic_old, "replacement_path": current_video}
        ],
    }
    deleted: list[str] = []

    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    result = cleanup.reconcile_video_poster_cleanup_receipts_locked(_Session(), job)

    assert result.ignored == 1
    assert result.deleted == 0
    assert deleted == []
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan


def test_per_job_cap_commits_tail_then_later_pass_drains_it(monkeypatch) -> None:
    job = _job({})
    cap = cleanup.VIDEO_POSTER_CLEANUP_RECEIPTS_PER_PASS
    replacements = [f"jobs/{job.id}/current-{index}.mp4" for index in range(cap + 2)]
    receipts = [
        {
            "old_path": _uuid_poster(f"jobs/{job.id}/old-{index}.mp4"),
            "replacement_path": replacement,
        }
        for index, replacement in enumerate(replacements)
    ]
    untouched_tail = copy.deepcopy(receipts[cap:])
    job.assembly_plan = {
        "variants": [
            {"variant_id": str(index), "video_path": replacement}
            for index, replacement in enumerate(replacements)
        ],
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: copy.deepcopy(receipts),
    }
    session = _Session()
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda _path, *, timeout_s: True,
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    first = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert first.receipts_seen == cap
    assert first.deleted == cap
    assert first.retained == 2
    assert not first.ok
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == untouched_tail
    assert deleted == [receipt["old_path"] for receipt in receipts[:cap]]
    assert session.commits == 1

    second = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert second.receipts_seen == 2
    assert second.deleted == 2
    assert second.retained == 0
    assert second.ok
    assert cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan
    assert deleted == [receipt["old_path"] for receipt in receipts]
    assert session.commits == 2


def test_permanent_head_failures_rotate_so_later_receipt_is_processed(monkeypatch) -> None:
    job = _job({})
    replacement = f"jobs/{job.id}/current.mp4"
    old_poster = _uuid_poster(f"jobs/{job.id}/old.mp4")
    valid_receipt = {"old_path": old_poster, "replacement_path": replacement}
    malformed = [None, {"old_path": old_poster, "replacement_path": None}]
    job.assembly_plan = {
        "output_path": replacement,
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [*malformed, valid_receipt],
    }
    session = _Session()
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.storage.object_exists_once",
        lambda _path, *, timeout_s: True,
    )
    monkeypatch.setattr(
        "app.storage.delete_object_once",
        lambda path, *, timeout_s: deleted.append(path) or True,
    )

    first = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert first.failures == 2
    assert first.retained == 3
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        valid_receipt,
        *malformed,
    ]

    second = cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert second.deleted == 1
    assert deleted == [old_poster]
    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        malformed[1],
        malformed[0],
    ]


@pytest.mark.parametrize("failing_operation", ["old_head", "replacement_head", "delete"])
def test_soft_time_limit_propagates_without_clearing_receipt(
    monkeypatch,
    failing_operation: str,
) -> None:
    job = _job({})
    replacement = f"jobs/{job.id}/current.mp4"
    old_poster = _uuid_poster(f"jobs/{job.id}/old.mp4")
    receipt = {"old_path": old_poster, "replacement_path": replacement}
    job.assembly_plan = {
        "output_path": replacement,
        cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD: [receipt],
    }
    session = _Session()
    deleted: list[str] = []
    monkeypatch.setattr(cleanup, "flag_modified", lambda *_args: None)

    def _exists(path: str, *, timeout_s: float) -> bool:
        if (failing_operation == "old_head" and path == old_poster) or (
            failing_operation == "replacement_head" and path == replacement
        ):
            raise SoftTimeLimitExceeded()
        return True

    def _delete(path: str, *, timeout_s: float) -> bool:
        deleted.append(path)
        if failing_operation == "delete":
            raise SoftTimeLimitExceeded()
        return True

    monkeypatch.setattr("app.storage.object_exists_once", _exists)
    monkeypatch.setattr("app.storage.delete_object_once", _delete)

    with pytest.raises(SoftTimeLimitExceeded):
        cleanup.reconcile_video_poster_cleanup_receipts_locked(session, job)

    assert job.assembly_plan[cleanup.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [receipt]
    assert session.commits == 0
    assert deleted == ([old_poster] if failing_operation == "delete" else [])
