from __future__ import annotations

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
