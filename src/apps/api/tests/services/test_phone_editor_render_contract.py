from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.routes import generative_jobs as gj
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_record, device_status
from tests.routes.test_phone_editor_commit import phone_job, save


def _binding_digest(binding: dict) -> str:
    encoded = json.dumps(
        binding["brief"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _strict_binding(seconds: int) -> dict:
    return {
        "brief": {
            "requirements": [
                {
                    "id": "length",
                    "kind": "timing",
                    "scope": "global",
                    "description": f"Keep it under {seconds} seconds",
                    "facts": {"duration_s": seconds},
                }
            ]
        }
    }


def test_second_manual_edit_inherits_the_prior_generation_brief_binding() -> None:
    from app.services.phone_editor import _rebind_editor_render_contract, _StagedJob

    inherited = _strict_binding(40)
    contract = CreatorRenderContract(
        generation_id="B", brief_digest=_binding_digest(inherited)
    ).rebind()
    job = SimpleNamespace(
        assembly_plan={
            "variants": [{"variant_id": "v"}],
            CONTRACT_REVISIONS_FIELD: {"v": contract.model_dump(mode="json")},
            "creator_brief_bindings": {"B": inherited},
        },
        all_candidates={},
    )

    staged = _StagedJob(job)
    _rebind_editor_render_contract(
        staged, "v", {"generation": "C", "sections": {}}, previous_variant={}
    )

    bindings = staged.assembly_plan["creator_brief_bindings"]
    assert bindings["C"] == inherited
    revised = read_render_contract(
        {CONTRACT_FIELD: staged.assembly_plan[CONTRACT_REVISIONS_FIELD]["v"]}
    )
    assert revised is not None and revised.generation_id == "C"
    assert revised.brief_digest == _binding_digest(bindings["C"])


def test_explicit_manual_brief_replacement_overrides_the_inherited_binding() -> None:
    from app.services.phone_editor import _rebind_editor_render_contract, _StagedJob

    inherited, replacement = _strict_binding(40), _strict_binding(30)
    contract = CreatorRenderContract(
        generation_id="B", brief_digest=_binding_digest(inherited)
    ).rebind()
    job = SimpleNamespace(
        assembly_plan={
            "variants": [{"variant_id": "v"}],
            CONTRACT_REVISIONS_FIELD: {"v": contract.model_dump(mode="json")},
            "creator_brief_bindings": {"B": inherited},
        },
        all_candidates={},
    )

    staged = _StagedJob(job)
    _rebind_editor_render_contract(
        staged,
        "v",
        {"generation": "C", "sections": {}},
        brief_binding=replacement,
        previous_variant={},
    )

    assert staged.assembly_plan["creator_brief_bindings"]["C"] == replacement


def test_tampered_prior_generation_binding_fails_instead_of_falling_back_to_root() -> None:
    from app.services.phone_editor import _rebind_editor_render_contract, _StagedJob

    root, tampered = _strict_binding(40), _strict_binding(30)
    contract = CreatorRenderContract(generation_id="B", brief_digest=_binding_digest(root)).rebind()
    staged = _StagedJob(
        SimpleNamespace(
            assembly_plan={
                "variants": [{"variant_id": "v"}],
                CONTRACT_REVISIONS_FIELD: {"v": contract.model_dump(mode="json")},
                "creator_brief_binding": root,
                "creator_brief_bindings": {"B": tampered},
            },
            all_candidates={},
        )
    )

    with pytest.raises(CreatorRenderContractError, match="approved brief"):
        _rebind_editor_render_contract(
            staged, "v", {"generation": "C", "sections": {}}, previous_variant={}
        )


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


def test_an_edit_that_moves_the_opening_text_out_of_the_opening_window_is_accepted(monkeypatch):
    """KRI-736: the creator's own placement wins; the words stay a required presence check."""
    job = phone_job(monkeypatch)
    _opening_contract(job)

    save(job, text="Before", start_s=1.0, end_s=3.0)  # text unchanged, moved later

    assert [(t.role, t.text) for t in _revised(job).exact_texts] == [("any", "Before")]
    assert device_status(job, "guided_story").request.identity.recipe_revision == 2


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


def test_the_duplicate_shape_relaxes_an_opening_title_the_creator_pushed_out_of_its_window(
    monkeypatch,
):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)

    save(job, text="Before", start_s=1.0, end_s=3.0)

    # [opening X (2.0 s), any X] collapses to the one presence check, never to nothing.
    assert [(t.role, t.text, t.duration_s) for t in _revised(job).exact_texts] == [
        ("any", "Before", None)
    ]


def test_the_duplicate_shape_keeps_both_requirements_through_a_retyped_title(monkeypatch):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)

    save(job, text="After", start_s=0, end_s=3)

    assert [(t.role, t.text, t.duration_s) for t in _revised(job).exact_texts] == [
        ("opening", "After", 2.0),
        ("any", "After", None),
    ]


def test_a_hold_the_creator_shortened_below_the_approved_minimum_is_accepted(monkeypatch):
    job = phone_job(monkeypatch)
    _prod_shape_contract(job)

    save(job, text="Before", start_s=0, end_s=1.0)  # a 1 s hold under a 2.0 s approval

    assert [(t.role, t.text, t.duration_s) for t in _revised(job).exact_texts] == [
        ("any", "Before", None)
    ]


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


def test_authored_timeline_save_rebinds_the_creator_length_in_the_new_generation(monkeypatch):
    from app.services.length_policy_state import read_length_policy, stamp_length_policy
    from tests.routes.test_phone_editor_commit import _enable_guided_v2

    job = phone_job(monkeypatch)
    _enable_guided_v2(job, monkeypatch)
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "creator_duration_s": 3.0,
    }
    contract = build_render_contract(strategy, generation_id="first")
    job.all_candidates = {"creator_strategy": strategy}
    job.assembly_plan = stamp_length_policy(
        {**job.assembly_plan, CONTRACT_FIELD: contract.model_dump(mode="json")},
        strategy,
        contract,
        mode="enforce",
    )
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    segment = revision["segments"][0]
    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=segment["segment_id"], clip_index=0, in_s=2.2, duration_s=0.8
                )
            ],
        ),
    )
    revised = _revised(job)
    assert revised.duration_s == pytest.approx(0.8)
    assert revised.generation_id == prep["generation"]
    assert read_length_policy(job.assembly_plan, revised).requested_duration_s == pytest.approx(0.8)
    assert read_length_policy(job.assembly_plan, contract).requested_duration_s == 3.0
    assert device_status(job, "guided_story").request.recipe.duration == pytest.approx(0.8)
