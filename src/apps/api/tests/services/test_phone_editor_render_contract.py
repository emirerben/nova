from __future__ import annotations

from app.services.creator_render_contract import (
    CONTRACT_FIELD,
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
