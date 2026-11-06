"""Already-authored state retains the original canonical reader requirement."""

import copy

import pytest
from fastapi import HTTPException

from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from tests.routes.test_phone_editor_rendered_clock import rendered_clock_job


@pytest.mark.parametrize("entry", ["ordinary", "authored"])
@pytest.mark.parametrize("protocol", [None, 25, 26])
def test_authored_preparation_keeps_rendered_reader_floor(monkeypatch, entry, protocol):
    clear_request_context()
    job = rendered_clock_job(monkeypatch)
    variant = gj._find_variant(job, "guided_story")
    staged = stage_initial_authored_baseline(
        job, "guided_story", gj.editor_deletion_timeline(job, variant)
    )
    before = copy.deepcopy(staged.assembly_plan)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    payload = gj.EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        text_elements=[],
        caption_meta={"enabled": True, "style": "sentence"},
    )

    def prepare():
        if entry == "ordinary":
            return gj.prepare_editor_commit(staged, "guided_story", payload)
        return prepare_authored_editor_commit(
            staged, "guided_story", payload, validation_arguments={}
        )

    set_client_protocol(protocol)
    try:
        if protocol != 26:
            with pytest.raises(HTTPException) as error:
                prepare()
            assert error.value.status_code == 426
            assert error.value.detail["feature"] == "rendered_timeline_clock"
            assert staged.assembly_plan == before
        else:
            result = prepare()
            assert result["render_destination"] == "device"
            assert result["sections"]["caption_meta"]
    finally:
        clear_request_context()
