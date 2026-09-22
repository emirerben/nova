"""Exercise admission completion and retry fencing with storage/DB boundaries mocked."""

import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import CreationThread, CreationThreadUploadReservation, Job, PlanItem, PlanItemAsset
from app.routes import generative_jobs
from app.services.phone_editor_sources import (
    EDITOR_SOURCES_FIELD,
    begin_attempt,
    merge_editor_sources,
)
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.tasks import editor_sources as task
from tests.services.test_phone_editor_sources import source
from tests.services.test_phone_sources import receipt


@pytest.fixture
def world(monkeypatch):
    user_id, item_id, job_id, asset_id = [uuid.uuid4() for _ in range(4)]
    imported = source(str(asset_id), lane="asset", kind="image", duration_s=None)
    asset = SimpleNamespace(
        id=asset_id,
        user_id=user_id,
        plan_item_id=item_id,
        status="ready",
        kind="image",
        gcs_path=imported["gcs_path"],
        gcs_generation="42",
    )
    record = dict(
        source_kind="visual",
        source_id=str(asset_id),
        user_id=str(user_id),
        base_generation="first",
        guided_revision_number=1,
    )
    token = begin_attempt(record)
    variant = {
        "variant_id": "v",
        "render_generation_id": "first",
        EDITOR_SOURCES_FIELD: {"imports": {"import": record}, "sources": []},
    }
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        content_plan_item_id=item_id,
        assembly_plan={"variants": [variant], "guided_edit": {"immutable": True}},
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    objects = {Job: job, PlanItem: item, PlanItemAsset: asset}
    commits = Mock()

    class Session:
        def get(self, model, *args, **kwargs):
            return objects.get(model)

        def execute(self, query):
            model = query.column_descriptions[0]["entity"]
            return SimpleNamespace(scalar_one_or_none=lambda: objects.get(model))

        def commit(self):
            commits()

    @contextmanager
    def session():
        yield Session()

    monkeypatch.setattr(task, "sync_session", session)
    monkeypatch.setattr(task, "flag_modified", Mock())
    monkeypatch.setattr(
        task.storage, "object_metadata", lambda path: SimpleNamespace(generation="42")
    )
    monkeypatch.setattr(generative_jobs, "_phone_editor_media_available", lambda *args: True)
    monkeypatch.setattr(
        generative_jobs,
        "_guided_v2_revision",
        lambda job, variant: {
            "revision_number": 1,
            "sources": merge_editor_sources([source()], variant),
        },
    )
    binding = {
        "media_id": str(asset_id),
        "kind": "image",
        "gcs_path": imported["gcs_path"],
        "generation": "42",
        "sha256": "a" * 64,
        "byte_count": 10,
    }
    prepare = Mock(return_value=(imported, binding))
    monkeypatch.setattr(task, "_prepare_visual", prepare)
    return SimpleNamespace(
        job=job,
        variant=variant,
        asset=asset,
        item=item,
        objects=objects,
        token=token,
        source=imported,
        binding=binding,
        prepare=prepare,
        commits=commits,
        session=session,
    )


def run(world, token=None):
    task.prepare_phone_editor_source.run(str(world.job.id), "v", "import", token or world.token)


def result(world):
    return world.variant[EDITOR_SOURCES_FIELD]["imports"]["import"]


def test_admit_once_preserves_approval_and_baseline(world):
    approval = copy.deepcopy(world.job.assembly_plan["guided_edit"])
    run(world)
    assert result(world)["status"] == "ready"
    assert result(world)["source_index"] == 1
    assert result(world)["source"] == world.source
    run(world)
    assert len(world.variant[EDITOR_SOURCES_FIELD]["sources"]) == 1
    world.prepare.assert_called_once()
    assert world.job.assembly_plan["guided_edit"] == approval
    assert world.variant["render_generation_id"] == "first"


@pytest.mark.parametrize(
    "field,value",
    [
        ("gcs_generation", "43"),
        ("status", "removed"),
        ("gcs_path", "users/u/replaced.jpg"),
        ("kind", "video"),
        ("user_id", uuid.UUID(int=9)),
        ("plan_item_id", uuid.UUID(int=8)),
    ],
)
def test_source_replaced_during_preparation_fails_atomically(world, field, value):
    def prepare(*args):
        setattr(world.asset, field, value)
        return world.source, world.binding

    world.prepare.side_effect = prepare
    run(world)
    assert result(world)["reason_code"] == "visual_changed"
    assert not result(world)["retryable"]
    assert world.variant[EDITOR_SOURCES_FIELD]["sources"] == []


@pytest.mark.parametrize("change", ["baseline", "current_job", "rollout", "generation"])
def test_final_fences_reject_changed_context(world, monkeypatch, change):
    def prepare(*args):
        if change == "baseline":
            world.variant["render_generation_id"] = "other"
        elif change == "current_job":
            world.item.current_job_id = uuid.uuid4()
        elif change == "rollout":
            monkeypatch.setattr(
                generative_jobs, "_phone_editor_media_available", lambda *args: False
            )
        else:
            monkeypatch.setattr(
                task.storage, "object_metadata", lambda path: SimpleNamespace(generation="43")
            )
        return world.source, world.binding

    world.prepare.side_effect = prepare
    run(world)
    assert result(world)["status"] == "failed"
    assert not result(world)["retryable"]
    assert world.variant[EDITOR_SOURCES_FIELD]["sources"] == []


@pytest.mark.parametrize("fail", [False, True])
def test_old_attempt_cannot_complete_or_fail_new_retry(world, fail):
    tokens = []

    def prepare(*args):
        tokens.append(begin_attempt(result(world)))
        if fail:
            raise RuntimeError("private exception content")
        return world.source, world.binding

    world.prepare.side_effect = prepare
    run(world)
    assert result(world)["attempt_id"] == tokens[0]
    assert result(world)["status"] == "preparing"
    assert world.variant[EDITOR_SOURCES_FIELD]["sources"] == []
    world.commits.assert_not_called()


@pytest.mark.parametrize(
    "exception,retryable,code",
    [
        (ValueError("users/private/path receipt"), False, "source_validation_failed"),
        (RuntimeError("secret network detail"), True, "source_prepare_failed"),
        (task.AdmissionError("source_codec_unsupported"), False, "source_codec_unsupported"),
    ],
)
def test_failures_have_safe_codes_and_retry_policy(world, exception, retryable, code):
    world.prepare.side_effect = exception
    run(world)
    assert result(world)["error"] == code
    assert result(world)["retryable"] is retryable
    assert world.variant[EDITOR_SOURCES_FIELD]["sources"] == []


def test_approved_source_reuses_index_and_receipt_without_duplicate(world):
    registry = {"sources": []}
    assembly = {PHONE_VISUALS_FIELD: [world.binding]}
    index, row = task._admit(
        registry,
        catalog=[world.source],
        source=world.source,
        receipt_key="visual_binding",
        receipt=world.binding,
        assembly=assembly,
    )
    assert index == 0 and row == world.source
    assert registry["sources"] == []


def test_conflicting_existing_receipt_is_rejected(world):
    registry = {"sources": []}
    with pytest.raises(task.AdmissionError, match="identity_conflict"):
        task._admit(
            registry,
            catalog=[world.source],
            source=world.source,
            receipt_key="visual_binding",
            receipt=world.binding,
            assembly={PHONE_VISUALS_FIELD: [{**world.binding, "sha256": "b" * 64}]},
        )
    assert registry["sources"] == []


def test_two_imports_same_source_share_index(world):
    run(world)
    token = begin_attempt(result(world))
    run(world, token)
    assert result(world)["source_index"] == 1
    assert len(world.variant[EDITOR_SOURCES_FIELD]["sources"]) == 1


def test_two_distinct_completions_append_indices_without_reuse(world):
    registry = {"sources": []}
    first, _ = task._admit(
        registry,
        catalog=[source()],
        source=world.source,
        receipt_key="visual_binding",
        receipt=world.binding,
        assembly={},
    )
    catalog = merge_editor_sources([source()], {EDITOR_SOURCES_FIELD: registry})
    second_source = {**world.source, "media_id": str(uuid.uuid4())}
    second, _ = task._admit(
        registry,
        catalog=catalog,
        source=second_source,
        receipt_key="visual_binding",
        receipt={**world.binding, "media_id": second_source["media_id"]},
        assembly={},
    )
    assert (first, second) == (1, 2)


def test_footage_probe_produces_valid_clip_source_and_rejects_codec(world, monkeypatch):
    raw = receipt()
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=world.job.user_id,
        object_path=raw["gcs_path"],
        media_id="one",
        upload_contract=raw["upload_contract"],
    )
    world.objects[CreationThreadUploadReservation] = reservation
    monkeypatch.setattr(
        task.storage, "signed_get_url_for_generation", lambda *args, **kwargs: "signed-url"
    )
    monkeypatch.setattr(
        task.storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=1000)
    )
    probe = SimpleNamespace(
        width=640,
        height=360,
        fps=15,
        rotation_degrees=0,
        duration_s=10,
        has_audio=True,
        codec="h264",
        pix_fmt="yuv420p",
    )
    monkeypatch.setattr(task, "probe_video", lambda url: probe)
    record = {"reservation_id": str(reservation.id), "user_id": str(world.job.user_id)}
    row, binding = task._prepare_footage(record)
    assert row["lane"] == "clip"
    assert task.canonical_source(row) == row
    assert binding.original.sha256 == "a" * 64
    probe.codec = "vp9"
    with pytest.raises(task.AdmissionError, match="source_codec_unsupported"):
        task._prepare_footage(record)


def test_final_footage_recheck_rejects_other_project(world):
    raw = receipt()
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=world.job.user_id,
        object_path=raw["gcs_path"],
        media_id="one",
        thread_id=uuid.uuid4(),
        upload_contract=raw["upload_contract"],
    )
    world.objects[CreationThreadUploadReservation] = reservation
    world.objects[CreationThread] = SimpleNamespace(
        creator_id=world.job.user_id, active_plan_item_id=uuid.uuid4()
    )
    record = {"source_kind": "footage", "source_id": "one", "reservation_id": str(reservation.id)}
    with (
        world.session() as db,
        pytest.raises(task.AdmissionError, match="source_reservation_invalid"),
    ):
        task._recheck_source(db, world.job, record, source("one", gcs_path=raw["gcs_path"]), {})
