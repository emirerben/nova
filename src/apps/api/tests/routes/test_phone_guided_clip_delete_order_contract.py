"""KRI-736: Save after deleting a clip from a guided story whose request fixed the clip order.

Deleting a clip converts a guided story into an AUTHORED timeline
(``stage_initial_authored_baseline``), which has no guided revision. The KRI-470 contract
rebind then found no source ids for the cut and refused an ``order_required`` contract, so
every such Save was a 422 ``unsupported_phone_edit``. Drives the exact route sequence
(``plan_items.editor_commit``) on a phone guided job with a real contract.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from tests.routes.test_phone_guided_timeline_commit import _guided_phone_job

VARIANT = "guided_story"
ORDER = ["m0", "m1", "m2", "m3"]


def _order_required_job(monkeypatch, *, order_required=True):
    job, variant, revision = _guided_phone_job(monkeypatch)
    contract = build_render_contract({}, generation_id="first", clip_order=ORDER)
    assert contract is not None
    if order_required:
        contract = contract.rebind(
            order_ids=tuple(ORDER), order_required=True, order_basis="capture_time"
        )
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    return job, variant, revision


def _save_after_deleting(job, variant, revision, deleted_media, *, reorder=False):
    """The `deletions` + full `timeline_slots` request the editor sends."""
    rows = gj.editor_deletion_timeline(job, variant)
    sources = revision["sources"]
    target = next(r for r in rows if sources[r["clip_index"]]["media_id"] == deleted_media)
    slots = [
        gj.TimelineSlotEdit(
            slot_id=r["slot_id"],
            clip_index=r["clip_index"],
            in_s=r["in_s"],
            duration_s=r["duration_s"],
            transition_after=r.get("transition_after") or "cut",
            transition_duration_s=r.get("transition_duration_s"),
        )
        for r in rows
    ]
    if reorder:
        slots.reverse()
    baseline = stage_initial_authored_baseline(job, VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        deletions=[{"kind": "clip", "id": target["slot_id"]}],
        timeline_slots=slots,
    )
    body = gj.normalize_editor_deletion_request(baseline, VARIANT, body)
    gj.require_guided_story_editor_commit(baseline, VARIANT, body)
    prep = prepare_authored_editor_commit(baseline, VARIANT, body, validation_arguments={})
    return baseline, prep


def _saved_contract(job_like):
    return job_like.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]


@pytest.mark.parametrize("deleted", ["m0", "m1", "m3"])
def test_delete_a_clip_saves_and_drops_its_media_from_the_required_order(monkeypatch, deleted):
    job, variant, revision = _order_required_job(monkeypatch)
    baseline, prep = _save_after_deleting(job, variant, revision, deleted)

    contract = _saved_contract(baseline)
    assert contract["order_required"] is True
    assert contract["order_ids"] == [m for m in ORDER if m != deleted]
    assert prep["authored_timeline"] is True
    # The authored cut really recompiled onto the device with only the survivors.
    clips = device_status(baseline, VARIANT).request.recipe.tracks[0].clips
    assert len(clips) == len(ORDER) - 1


def test_a_reordered_cut_is_recorded_in_its_new_order(monkeypatch):
    job, variant, revision = _order_required_job(monkeypatch)
    baseline, _ = _save_after_deleting(job, variant, revision, "m1", reorder=True)
    # The reversed cut is approved against ITS order, never against the capture order.
    assert _saved_contract(baseline)["order_ids"] == ["m3", "m2", "m0"]


def test_an_unidentified_slot_still_refuses_an_order_required_contract(monkeypatch):
    job, variant, revision = _order_required_job(monkeypatch)
    from app.services import authored_editor

    real = authored_editor.resolve_authored_phone_slots

    def without_media_id(*args, **kwargs):
        rows = real(*args, **kwargs)
        rows[0].pop("media_id")
        return rows

    monkeypatch.setattr(authored_editor, "resolve_authored_phone_slots", without_media_id)
    with pytest.raises((CreatorRenderContractError, HTTPException)) as raised:
        _save_after_deleting(job, variant, revision, "m1")
    assert "source-identified" in str(
        raised.value.detail if hasattr(raised.value, "detail") else raised.value
    )


def test_a_contract_without_a_required_order_is_not_forced_into_one(monkeypatch):
    job, variant, revision = _order_required_job(monkeypatch, order_required=False)
    baseline, _ = _save_after_deleting(job, variant, revision, "m1")
    assert _saved_contract(baseline)["order_required"] is False
