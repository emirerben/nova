"""Ownership, bounded-I/O, and concurrent-merge tests for clip metadata."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import ContentPlan, PlanItem
from app.tasks import creator_clip_metadata as task_module


@pytest.fixture(autouse=True)
def _stable_object_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_module, "_object_generation", lambda _path: "generation-1")


class _Session:
    def __init__(self, item, plan):  # noqa: ANN001
        self.item = item
        self.plan = plan
        self.commit = MagicMock()
        self.rollback = MagicMock()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _pk, **_kwargs):
        return {PlanItem: self.item, ContentPlan: self.plan}.get(model)


def _rows(*, count: int = 2, assignments: bool = True):
    owner_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    paths = [f"users/{owner_id}/plan/{item_id}/clip-{index}.mp4" for index in range(count)]
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        clip_gcs_paths=list(paths),
        clip_assignments=(
            [
                {
                    "gcs_path": path,
                    "shot_id": None,
                    "user_note": f"note-{index}",
                    "machine_matched": False,
                    "media_id": f"media-{index}",
                }
                for index, path in enumerate(paths)
            ]
            if assignments
            else []
        ),
    )
    plan = SimpleNamespace(
        id=plan_id,
        user_id=owner_id,
        ownership_epoch=7,
        ownership_quarantined_at=None,
    )
    return item, plan, paths


def _sessions(monkeypatch: pytest.MonkeyPatch, item, plan):  # noqa: ANN001
    created: list[_Session] = []

    def factory():
        session = _Session(item, plan)
        created.append(session)
        return session

    monkeypatch.setattr(task_module, "sync_session", factory)
    return created


def test_hydrates_all_fifty_assignments_without_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, plan, _paths = _rows(count=50)
    _sessions(monkeypatch, item, plan)
    probe = MagicMock(return_value=2.5)
    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)

    assert len(item.clip_assignments) == 50
    assert probe.call_count == 50
    assert all(assignment["duration_s"] == 2.5 for assignment in item.clip_assignments)


def test_foreign_jsonb_path_is_never_signed_or_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    item, plan, _paths = _rows(count=1)
    item.clip_gcs_paths = ["users/another-user/plan/secret/video.mp4"]
    item.clip_assignments[0]["gcs_path"] = item.clip_gcs_paths[0]
    _sessions(monkeypatch, item, plan)
    probe = MagicMock()
    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)

    probe.assert_not_called()
    assert "duration_s" not in item.clip_assignments[0]


def test_probe_results_merge_into_latest_assignments_without_resurrecting_removed_clips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, plan, paths = _rows(count=2)
    _sessions(monkeypatch, item, plan)
    replacement_path = f"users/{plan.user_id}/plan/{item.id}/replacement.mp4"

    def probe(path: str) -> float:
        if path == paths[0]:
            item.clip_gcs_paths = [paths[1], replacement_path]
            item.clip_assignments = [
                {**item.clip_assignments[1], "user_note": "changed concurrently"},
                {
                    "gcs_path": replacement_path,
                    "shot_id": None,
                    "user_note": "new clip",
                    "machine_matched": False,
                    "media_id": "replacement",
                },
            ]
        return 3.25

    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)

    assert [entry["gcs_path"] for entry in item.clip_assignments] == [paths[1], replacement_path]
    assert item.clip_assignments[0]["user_note"] == "changed concurrently"
    assert item.clip_assignments[0]["duration_s"] == 3.25
    assert "duration_s" not in item.clip_assignments[1]


def test_legacy_clip_paths_gain_stable_assignments_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, plan, paths = _rows(count=2, assignments=False)
    sessions = _sessions(monkeypatch, item, plan)
    monkeypatch.setattr(task_module, "_probe_clip_duration_s", lambda _path: 4.5)

    task_module._run(str(item.id), expected_ownership_epoch=7)

    assert [entry["gcs_path"] for entry in item.clip_assignments] == paths
    assert all(entry.get("media_id") for entry in item.clip_assignments)
    assert all(entry.get("duration_s") == 4.5 for entry in item.clip_assignments)
    sessions[0].commit.assert_called_once()


def test_epoch_change_during_probe_discards_results(monkeypatch: pytest.MonkeyPatch) -> None:
    item, plan, _paths = _rows(count=1)
    _sessions(monkeypatch, item, plan)

    def probe(_path: str) -> float:
        plan.ownership_epoch += 1
        return 8.0

    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)

    assert "duration_s" not in item.clip_assignments[0]


def test_duplicate_deliveries_probe_each_object_generation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, plan, _paths = _rows(count=2)
    _sessions(monkeypatch, item, plan)
    probe = MagicMock(return_value=6.0)
    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)
    task_module._run(str(item.id), expected_ownership_epoch=7)

    assert probe.call_count == 2
    assert all(entry["duration_probe_status"] == "ready" for entry in item.clip_assignments)


def test_failed_probe_backs_off_until_object_generation_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, plan, _paths = _rows(count=1)
    _sessions(monkeypatch, item, plan)
    generation = ["generation-1"]
    monkeypatch.setattr(task_module, "_object_generation", lambda _path: generation[0])
    probe = MagicMock(return_value=None)
    monkeypatch.setattr(task_module, "_probe_clip_duration_s", probe)

    task_module._run(str(item.id), expected_ownership_epoch=7)
    task_module._run(str(item.id), expected_ownership_epoch=7)
    assert probe.call_count == 1
    assert item.clip_assignments[0]["duration_probe_status"] == "failed"

    generation[0] = "generation-2"
    task_module._run(str(item.id), expected_ownership_epoch=7)
    assert probe.call_count == 2
    assert item.clip_assignments[0]["duration_probe_generation"] == "generation-2"


def test_task_is_pinned_to_dedicated_analysis_queue() -> None:
    assert task_module.analyze_creator_clip_metadata._get_exec_options()["queue"] == (
        task_module.CREATOR_CLIP_METADATA_QUEUE
    )


def test_task_limits_cover_the_bounded_fifty_clip_probe() -> None:
    options = task_module.analyze_creator_clip_metadata._get_exec_options()

    assert options["soft_time_limit"] >= (
        task_module._MAX_CLIPS_PER_ITEM * task_module._PER_CLIP_WORST_CASE_S + 120
    )
    assert options["time_limit"] > options["soft_time_limit"]
    assert task_module._PROBE_LEASE_S > options["time_limit"]
