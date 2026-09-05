from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.tasks import generative_build


def _cancelled_job(**overrides):
    values = {
        "id": uuid.uuid4(),
        "status": "cancelled",
        "mode": "generative",
        "assembly_plan": {
            "variants": [
                {
                    "variant_id": "old",
                    "render_status": "ready",
                    "video_path": "generative-jobs/existing/old.mp4",
                }
            ]
        },
        "all_candidates": {"sentinel": "unchanged"},
        "error_detail": "cancel audit",
        "failure_reason": "cancelled_by_admin",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session_context(job):
    session = MagicMock()
    session.get.return_value = job

    @contextmanager
    def factory():
        yield session

    return factory, session


@pytest.mark.parametrize(
    "operation",
    [
        lambda job_id: generative_build._set_status(  # noqa: SLF001
            job_id, "variants_ready", extra_plan={"new": "value"}
        ),
        lambda job_id: generative_build._fail_job(  # noqa: SLF001
            job_id, "late failure", failure_reason="late"
        ),
        lambda job_id: generative_build._upsert_variant_entry(  # noqa: SLF001
            job_id, {"variant_id": "new", "video_path": "unexpected"}
        ),
        lambda job_id: generative_build._update_variant_entry(  # noqa: SLF001
            job_id, "old", {"render_status": "failed", "video_path": "unexpected"}
        ),
    ],
)
def test_cancelled_job_rejects_shared_mutation_as_whole_operation(operation) -> None:
    job = _cancelled_job()
    before = copy.deepcopy(vars(job))
    session_factory, session = _session_context(job)

    with patch.object(generative_build, "_sync_session", session_factory):
        assert operation(str(job.id)) is False

    assert vars(job) == before
    session.commit.assert_not_called()
    assert session.get.call_args.kwargs["with_for_update"] is True


def test_cancelled_late_finalization_preserves_row_and_deletes_only_fresh_outputs() -> None:
    job_id = uuid.uuid4()
    old_path = f"generative-jobs/{job_id}/old.mp4"
    job = _cancelled_job(
        id=job_id,
        assembly_plan={
            "variants": [{"variant_id": "old", "render_status": "ready", "video_path": old_path}]
        },
    )
    before = copy.deepcopy(job.assembly_plan)
    session_factory, session = _session_context(job)
    fresh_video = f"generative-jobs/{job_id}/variant_1.mp4"
    fresh_base = f"generative-jobs/{job_id}/variant_1_base.mp4"
    fresh_matte = f"generative-jobs/{job_id}/variant_1_base.mp4.subject-matte-v2.mp4"
    deleted: list[str] = []

    with (
        patch.object(generative_build, "_sync_session", session_factory),
        patch(
            "app.storage.delete_object_best_effort",
            side_effect=lambda path: deleted.append(path) or True,
        ),
    ):
        accepted = generative_build._finalize_job(  # noqa: SLF001
            str(job_id),
            [
                {
                    "variant_id": "new",
                    "rank": 1,
                    "text_mode": "agent_text",
                    "ok": True,
                    "video_path": fresh_video,
                    "base_video_path": fresh_base,
                    "subject_matte_path": fresh_matte,
                }
            ],
        )

    assert accepted is False
    assert job.assembly_plan == before
    session.commit.assert_not_called()
    assert set(deleted) == {fresh_video, fresh_base, fresh_matte, f"{fresh_matte}.json"}
    assert old_path not in deleted


def test_cancelled_required_speech_finalization_retains_durable_generation_owner() -> None:
    """Required outputs are reconciled from their receipt, never deleted eagerly."""

    job_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    job = _cancelled_job(
        id=job_id,
        assembly_plan={"variants": []},
    )
    before = copy.deepcopy(job.assembly_plan)
    session_factory, session = _session_context(job)
    result = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "render_generation_id": generation,
        "render_status": "ready",
        "ok": True,
        "video_path": (f"generative-jobs/{job_id}/render-generations/{generation}/output.mp4"),
    }
    deleted: list[str] = []

    with (
        patch.object(generative_build, "_sync_session", session_factory),
        patch(
            "app.storage.delete_object_best_effort",
            side_effect=lambda path: deleted.append(path) or True,
        ),
    ):
        accepted = generative_build._finalize_job(  # noqa: SLF001
            str(job_id),
            [result],
            required_speech_results={"subtitled": result},
        )

    assert accepted is False
    assert job.assembly_plan == before
    assert deleted == []
    session.commit.assert_not_called()


def test_manual_draft_first_export_passes_owner_fence_and_promotes_job() -> None:
    """A linked draft is plan-owned even before its first render changes mode.

    Regression: the shared task fence used to require ``mode=content_plan`` for
    every linked Job, so manual exports were rejected before FFmpeg could run.
    """
    from app.models import ContentPlan, Persona, PlanItem

    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    user_id = uuid.uuid4()
    job = _cancelled_job(
        id=job_id,
        user_id=user_id,
        status="draft",
        mode="manual_draft",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=8,
        assembly_plan={
            "manual_draft": True,
            "variants": [{"variant_id": "original_text", "render_status": "rendering"}],
        },
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    plan = SimpleNamespace(
        id=plan_id,
        user_id=user_id,
        persona_id=persona_id,
        ownership_epoch=8,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=persona_id, user_id=user_id)
    session = MagicMock()

    def get(model, _pk, **_kwargs):
        return {
            generative_build.Job: job,
            PlanItem: item,
            ContentPlan: plan,
            Persona: persona,
        }[model]

    session.get.side_effect = get

    @contextmanager
    def session_factory():
        yield session

    token = generative_build._CONTENT_PLAN_FENCE.set((str(job_id), 8))  # noqa: SLF001
    try:
        with patch.object(generative_build, "_sync_session", session_factory):
            accepted = generative_build._update_variant_entry(  # noqa: SLF001
                str(job_id),
                "original_text",
                {
                    "render_status": "ready",
                    "video_path": f"generative-jobs/{job_id}/manual-export.mp4",
                },
            )
    finally:
        generative_build._CONTENT_PLAN_FENCE.reset(token)  # noqa: SLF001

    assert accepted is True
    assert job.mode == "content_plan"
    assert job.status == "variants_ready"
    assert job.assembly_plan["variants"][0]["render_status"] == "ready"
    session.commit.assert_called_once()


def test_failed_manual_export_remains_hidden_and_retryable() -> None:
    job_id = uuid.uuid4()
    job = _cancelled_job(
        id=job_id,
        status="draft",
        mode="manual_draft",
        content_plan_item_id=None,
        assembly_plan={
            "manual_draft": True,
            "variants": [{"variant_id": "original_text", "render_status": "rendering"}],
        },
    )
    session_factory, session = _session_context(job)

    with patch.object(generative_build, "_sync_session", session_factory):
        accepted = generative_build._update_variant_entry(  # noqa: SLF001
            str(job_id),
            "original_text",
            {"render_status": "failed", "error_class": "encoder_error"},
        )

    assert accepted is True
    assert job.mode == "manual_draft"
    assert job.status == "draft"
    assert job.assembly_plan["variants"][0]["render_status"] == "failed"
    session.commit.assert_called_once()


def test_quarantined_content_plan_job_exits_before_status_agents_or_storage() -> None:
    from datetime import UTC, datetime

    from app.models import ContentPlan, PlanItem

    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    user_id = uuid.uuid4()
    job = _cancelled_job(
        id=job_id,
        user_id=user_id,
        status="queued",
        mode="content_plan",
        content_plan_item_id=item_id,
        all_candidates={"clip_paths": ["users/example/raw.mp4"], "persona": {"stale": True}},
    )
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
    )
    plan = SimpleNamespace(
        id=plan_id,
        user_id=user_id,
        persona_id=uuid.uuid4(),
        ownership_epoch=7,
        ownership_quarantined_at=datetime.now(UTC),
    )
    session = MagicMock()

    def get(model, _pk, **_kwargs):
        if model is generative_build.Job:
            return job
        if model is PlanItem:
            return item
        if model is ContentPlan:
            return plan
        raise AssertionError(f"unexpected model load: {model}")

    session.get.side_effect = get

    @contextmanager
    def session_factory():
        yield session

    phase = MagicMock(side_effect=AssertionError("phase write must not run"))
    started = MagicMock(side_effect=AssertionError("phase start must not run"))
    ingest = MagicMock(side_effect=AssertionError("agent/storage work must not run"))
    durable = MagicMock(side_effect=AssertionError("storage copy must not run"))
    fail = MagicMock(side_effect=AssertionError("status failure write must not run"))
    with (
        patch.object(generative_build, "_sync_session", session_factory),
        patch.object(generative_build, "record_phase", phase),
        patch.object(generative_build, "mark_started", started),
        patch.object(generative_build, "_ingest_clips", ingest),
        patch.object(generative_build, "_persist_durable_sources", durable),
        patch.object(generative_build, "_fail_job", fail),
    ):
        generative_build._run_generative_job(str(job_id))  # noqa: SLF001

    assert job.status == "queued"
    assert job.all_candidates["persona"] == {"stale": True}
    session.commit.assert_not_called()
    phase.assert_not_called()
    started.assert_not_called()
    ingest.assert_not_called()
    durable.assert_not_called()
    fail.assert_not_called()


def test_content_plan_epoch_change_rejects_late_variant_write() -> None:
    from app.models import ContentPlan, Persona, PlanItem

    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    user_id = uuid.uuid4()
    job = _cancelled_job(
        id=job_id,
        user_id=user_id,
        status="rendering",
        mode="content_plan",
        content_plan_item_id=item_id,
        assembly_plan={"variants": [{"variant_id": "old", "render_status": "pending"}]},
    )
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
    )
    plan = SimpleNamespace(
        id=plan_id,
        user_id=user_id,
        persona_id=persona_id,
        ownership_epoch=8,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=persona_id, user_id=user_id)
    session = MagicMock()

    def get(model, _pk, **_kwargs):
        return {
            generative_build.Job: job,
            PlanItem: item,
            ContentPlan: plan,
            Persona: persona,
        }[model]

    session.get.side_effect = get

    @contextmanager
    def session_factory():
        yield session

    before = copy.deepcopy(job.assembly_plan)
    token = generative_build._CONTENT_PLAN_FENCE.set((str(job_id), 7))  # noqa: SLF001
    try:
        with patch.object(generative_build, "_sync_session", session_factory):
            accepted = generative_build._upsert_variant_entry(  # noqa: SLF001
                str(job_id), {"variant_id": "new", "render_status": "ready"}
            )
    finally:
        generative_build._CONTENT_PLAN_FENCE.reset(token)  # noqa: SLF001

    assert accepted is False
    assert job.assembly_plan == before
    session.commit.assert_not_called()


@pytest.mark.parametrize("kind", ["preprocessed", "hdr"])
def test_cancelled_cache_upload_rejection_deletes_every_exact_uploaded_key(kind: str) -> None:
    job_id = uuid.uuid4()
    job = _cancelled_job(id=job_id)
    before = copy.deepcopy(job.all_candidates)
    session_factory, session = _session_context(job)
    uploaded: list[str] = []
    deleted: list[str] = []

    with (
        patch.object(generative_build, "_sync_session", session_factory),
        patch(
            "app.storage.upload_public_read",
            side_effect=lambda _local, path, **_kwargs: uploaded.append(path) or "signed",
        ),
        patch(
            "app.storage.delete_object_best_effort",
            side_effect=lambda path: deleted.append(path) or True,
        ),
    ):
        if kind == "preprocessed":
            generative_build._store_preprocessed_source_cache(  # noqa: SLF001
                str(job_id), ["users/u/a.mp4", "users/u/b.mp4"], ["/tmp/a.mp4", "/tmp/b.mp4"]
            )
        else:
            generative_build._store_hdr_pretonemap_cache(  # noqa: SLF001
                str(job_id),
                signature={"clips": ["a", "b"]},
                converted=[
                    ("clip-a", "/tmp/a-tonemapped.mp4", {"hdr": True}),
                    ("clip-b", "/tmp/b-tonemapped.mp4", {"hdr": True}),
                ],
            )

    assert uploaded
    assert set(deleted) == set(uploaded)
    assert job.all_candidates == before
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    "snapshot_fields",
    [
        {"pre_media_overlay_video_path": "pre-media.mp4"},
        {"pre_sfx_video_path": "pre-sfx.mp4"},
        {
            "pre_media_overlay_video_path": "pre-media.mp4",
            "pre_sfx_video_path": "pre-sfx.mp4",
        },
    ],
)
def test_initial_cancelled_upsert_cleanup_includes_fresh_media_snapshots(
    snapshot_fields: dict[str, str],
) -> None:
    job_id = str(uuid.uuid4())
    result = {key: f"generative-jobs/{job_id}/{name}" for key, name in snapshot_fields.items()}
    deleted: list[str] = []
    with patch(
        "app.storage.delete_object_best_effort",
        side_effect=lambda path: deleted.append(path) or True,
    ):
        generative_build._discard_generation_storage(  # noqa: SLF001
            result,
            job_id=job_id,
            generation=None,
            fields=tuple(snapshot_fields),
        )

    assert set(deleted) == set(result.values())


def test_cancelled_object_delete_failure_is_high_severity() -> None:
    job_id = str(uuid.uuid4())
    path = f"generative-jobs/{job_id}/late.mp4"
    with (
        patch("app.storage.delete_object_best_effort", return_value=False),
        patch.object(generative_build.log, "error") as log_error,
    ):
        generative_build._delete_cancelled_job_objects(job_id, [path])  # noqa: SLF001

    log_error.assert_called_once_with(
        "cancelled_job_storage_delete_failed",
        job_id=job_id,
        object_path=path,
    )
