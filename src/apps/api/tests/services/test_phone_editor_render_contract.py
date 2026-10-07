from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_record, device_status
from tests.routes.test_phone_editor_commit import phone_job, save


def test_phone_title_save_rebinds_only_its_variant_contract(monkeypatch) -> None:
    job = phone_job(monkeypatch)
    root = build_render_contract({"opening_title": "Before"}, generation_id="first")
    assert root is not None
    job.assembly_plan[CONTRACT_FIELD] = root.model_dump(mode="json")

    save(job)

    # The original authority is still usable by a sibling variant; only the
    # saved phone variant receives its next-generation approval snapshot.
    assert read_render_contract(job.assembly_plan) == root
    revised = read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )
    assert revised is not None
    assert revised.generation_id == job.assembly_plan["variants"][0]["render_generation_id"]
    assert [item.text for item in revised.exact_texts] == ["After"]
    record = device_record(job, "guided_story")
    assert record["contract_digest"] == revised.digest
    assert device_status(job, "guided_story").request.identity.recipe_revision == 2


def test_new_talking_title_worker_satisfies_approved_contract(monkeypatch) -> None:
    from app.tasks import generative_build
    from tests.tasks.test_phone_subtitled_title_worker import _faces, _titled

    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _titled(monkeypatch)
    contract = build_render_contract(
        job.all_candidates["creator_strategy"], generation_id="generation"
    )
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    job.all_candidates[REQUIREMENT_VERSION_FIELD] = 1

    generative_build._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert device_record(job, "subtitled")["contract_digest"] == contract.digest


# --- KRI-470 PR-G: the editor keeps each approved text's role ---------------------


def _opening_contract(job, text="Before"):
    contract = build_render_contract({"opening_title": text}, generation_id="first")
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    return contract


def _revised(job):
    return read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )


def test_an_edit_that_keeps_the_opening_text_in_the_opening_window_is_accepted(monkeypatch):
    job = phone_job(monkeypatch)
    _opening_contract(job)

    save(job, text="After", start_s=0, end_s=3)

    revised = _revised(job)
    assert [(item.role, item.text) for item in revised.exact_texts] == [("opening", "After")]
    assert device_status(job, "guided_story").request.identity.recipe_revision == 2


def test_an_edit_that_moves_the_opening_text_out_of_the_opening_window_is_refused(monkeypatch):
    job = phone_job(monkeypatch)
    _opening_contract(job)
    before = device_status(job, "guided_story").request
    variants_before = copy.deepcopy(job.assembly_plan["variants"])

    with pytest.raises(HTTPException) as caught:
        save(job, text="Before", start_s=1.0, end_s=3.0)  # text unchanged, moved later

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "unsupported_phone_edit"
    # The refusal says what was violated, typed, so the client can explain it.
    assert caught.value.detail["decline_reason"] == "evidence_missing"
    assert caught.value.detail["field_path"] == "opening_title"
    # The refusal leaves the approved edit exactly as it was.
    assert device_status(job, "guided_story").request == before
    assert job.assembly_plan["variants"] == variants_before
    assert CONTRACT_REVISIONS_FIELD not in job.assembly_plan


def test_an_element_that_was_never_a_requirement_stays_unplaced(monkeypatch):
    job = phone_job(monkeypatch)
    _opening_contract(job)
    first = job.assembly_plan["variants"][0]["text_elements"][0]
    extra = {**first, "id": "added", "text": "Added later", "start_s": 1.0, "end_s": 2.0}

    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first", text_elements=[{**first, "text": "After"}, extra]
        ),
    )

    roles = {item.text: item.role for item in _revised(job).exact_texts}
    assert roles == {"After": "opening", "Added later": "any"}


# --- the standard prod shape: [opening X (2.0 s), any X] ------------------------------


def _prod_shape_contract(job, text="Before", hold=2.0):
    """Prod jobs 110c3dd2 / e1c5f89e: the strategy's opening title AND the brief's literal."""
    from app.kria.brief import BriefRequirement, CreativeBrief

    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="text", scope="title", literal=text)],
    )
    contract = build_render_contract(
        {"opening_title": text, "opening_title_duration_s": hold},
        generation_id="first",
        brief=brief,
    )
    # (No brief binding is stored on this fixture job, so drop the digest it would check.)
    contract = contract.rebind(brief_digest=None)
    assert [(t.role, t.duration_s) for t in contract.exact_texts] == [
        ("opening", hold),
        ("any", None),
    ]
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    return contract


def test_the_duplicate_shape_still_refuses_an_opening_title_pushed_out_of_its_window(
    monkeypatch,
):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)
    before = device_status(job, "guided_story").request

    with pytest.raises(HTTPException) as caught:
        save(job, text="Before", start_s=1.0, end_s=3.0)

    assert caught.value.detail["field_path"] == "opening_title"
    assert device_status(job, "guided_story").request == before


def test_the_duplicate_shape_keeps_both_requirements_through_a_retyped_title(monkeypatch):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)

    save(job, text="After", start_s=0, end_s=3)

    assert [(t.role, t.text, t.duration_s) for t in _revised(job).exact_texts] == [
        ("opening", "After", 2.0),
        ("any", "After", None),
    ]


def test_the_approved_on_screen_minimum_is_reverified_after_an_edit(monkeypatch):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)

    with pytest.raises(HTTPException) as caught:
        save(job, text="Before", start_s=0, end_s=1.0)  # a 1 s hold under a 2.0 s approval

    assert caught.value.detail["field_path"] == "opening_title_duration_s"


def test_a_row_the_contract_never_asked_for_stays_any_beside_the_duplicate_shape(monkeypatch):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)
    first = job.assembly_plan["variants"][0]["text_elements"][0]
    extra = {**first, "id": "added", "text": "Added later", "start_s": 1.0, "end_s": 2.0}

    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(base_generation="first", text_elements=[first, extra]),
    )

    assert sorted((t.role, t.text) for t in _revised(job).exact_texts) == [
        ("any", "Added later"),
        ("any", "Before"),
        ("opening", "Before"),
    ]
