from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models import Job, PlanItem
from app.services.edit_artifacts import load_render_capture_snapshot, split_for_capture


class _Db:
    def __init__(self, job, item):  # noqa: ANN001
        self.job = job
        self.item = item

    def get(self, model, _identity):  # noqa: ANN001
        if model is Job:
            return self.job
        if model is PlanItem:
            return self.item
        return None


def _eligible(creator_id):  # noqa: ANN001
    return SimpleNamespace(
        eligible=True,
        creator_id=creator_id,
        basis="internal_grant",
        consent_event_id=None,
        internal_grant_id=uuid.uuid4(),
    )


def test_capture_uses_canonical_revision_and_rejects_raw_upload(monkeypatch) -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=creator_id,
        content_plan_item_id=item_id,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "guided",
                    "render_status": "ready",
                    "ok": True,
                    "video_path": "generative-jobs/job/final.mp4",
                    "render_generation_id": "render-7",
                    "guided_edit_revision": {"state_hash": "saved-revision"},
                    "render_receipt": {"schema_version": "1"},
                    "duration_s": 14,
                }
            ]
        },
        probe_metadata={},
    )
    item = SimpleNamespace(id=item_id, edit_proposal={})
    monkeypatch.setattr(
        "app.services.edit_artifacts.resolve_training_eligibility",
        lambda *_args, **_kwargs: _eligible(creator_id),
    )

    snapshot = load_render_capture_snapshot(
        _Db(job, item),
        job_id=job.id,
        variant_id="guided",
        render_generation_id="render-7",
    )
    assert snapshot is not None
    assert snapshot.render_receipt["revision_hash"] == "saved-revision"
    assert snapshot.source_path == "generative-jobs/job/final.mp4"

    job.assembly_plan["variants"][0]["video_path"] = f"users/{creator_id}/plan/{item_id}/raw.mp4"
    assert load_render_capture_snapshot(_Db(job, item), job_id=job.id, variant_id="guided") is None


def test_creator_and_plan_item_never_cross_dataset_splits(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.edit_artifacts.settings.training_dataset_split_secret",
        "split-secret-at-least-sixteen",
    )
    creator_id = uuid.uuid4()
    splits = {split_for_capture(creator_id, uuid.uuid4()) for _ in range(20)}
    assert len(splits) == 1
    creator_split, plan_item_split = splits.pop()
    assert creator_split == plan_item_split
