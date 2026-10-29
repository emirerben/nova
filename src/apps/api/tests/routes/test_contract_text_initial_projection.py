"""First editor reads preserve the exact text plan the worker actually rendered."""

from __future__ import annotations

import copy

from app.routes import generative_jobs as gj
from app.schemas.guided_edit_revision import normalize_guided_editor_revision
from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD
from tests.routes._editor_save_matrix import guided_phone_job


def _worker_repaired_text(job, variant):
    canonical = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    repaired = copy.deepcopy(canonical)
    facts = copy.deepcopy(repaired["text_elements"][0])
    facts.update(id="contract-text-1", text="The creator's confirmed facts.", y_frac=0.55)
    repaired["text_elements"].append(facts)
    variant[PHONE_EDITOR_PLAN_FIELD] = repaired
    variant["text_elements"] = copy.deepcopy(repaired["text_elements"])
    return canonical, repaired


def test_initial_public_editor_projection_keeps_the_worker_added_literal(monkeypatch):
    job, variant, _ = guided_phone_job(monkeypatch)
    canonical, repaired = _worker_repaired_text(job, variant)
    projected = gj._guided_text_state_for_response(job, variant)
    assert projected is not None
    assert projected[0] == repaired["text_elements"]
    assert job.assembly_plan["guided_story_execution_plan"] == canonical
    assert variant[PHONE_EDITOR_PLAN_FIELD] == repaired


def test_saved_editor_revision_still_wins_over_the_initial_worker_repair(monkeypatch):
    job, variant, initial = guided_phone_job(monkeypatch)
    _worker_repaired_text(job, variant)
    saved = copy.deepcopy(initial)
    saved["text_elements"][0]["text"] = "A later manual edit"
    saved["revision_number"] += 1
    saved["state_hash"] = ""
    variant["guided_edit_revision"] = normalize_guided_editor_revision(saved)
    assert gj._guided_v2_revision(job, variant) == variant["guided_edit_revision"]
