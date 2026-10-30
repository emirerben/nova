"""Save the real guided-to-authored boundary without inventing a caption lane.

Failure modes: default caption presentation blocks an ordinary text Save; a
compatibility allowance admits actual caption rows or speech edits; metadata
vanishes on reopen; a stale or invalid Save mutates the last good project.
"""

import copy

import pytest
from fastapi import HTTPException

from app.kria.device_render import make_device_request
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.services.editor_empty_drafts import editor_sections, stage_initial_authored_baseline
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from tests.routes.test_phone_editor_commit import _enable_guided_v2, phone_job

META = {
    "style": "sentence",
    "enabled": True,
    "appearance": {"shadow_opacity": 0.0},
    "shadow_enabled": False,
}


def project(monkeypatch, enabled=True):
    job = phone_job(monkeypatch)
    _enable_guided_v2(job, monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    plan = GuidedStoryExecutionPlan.model_validate(job.assembly_plan["guided_story_execution_plan"])
    recipe = compile_phone_guided_plan(
        plan,
        [PhoneSourceBinding.model_validate(row) for row in job.assembly_plan[PHONE_SOURCES_FIELD]],
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="guided_story", revision=2, recipe=recipe),
        base_generation="first",
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", enabled)
    variant = gj._find_variant(job, "guided_story")
    staged = stage_initial_authored_baseline(
        job, "guided_story", gj.editor_deletion_timeline(job, variant)
    )
    return job, staged


def request(staged, *, meta=None, wire=2, **extra):
    variant = gj._find_variant(staged, "guided_story")
    return gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=wire,
        text_elements=copy.deepcopy(variant["text_elements"]),
        caption_meta=META if meta is None else meta,
        **extra,
    )


def prepare(staged, payload):
    return prepare_authored_editor_commit(staged, "guided_story", payload, validation_arguments={})


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("wire", [1, 2])
@pytest.mark.parametrize(
    "meta", [META, {"style": "word", "enabled": False, "font": "Inter", "font_set": True}]
)
def test_empty_caption_presentation_survives_authored_save_and_reopen(
    monkeypatch, enabled, wire, meta
):
    original, staged = project(monkeypatch, enabled)
    original_before = copy.deepcopy(original.assembly_plan)
    texts_before = copy.deepcopy(gj._find_variant(staged, "guided_story")["text_elements"])
    old_recipe = device_status(staged, "guided_story").request.recipe

    submitted = request(staged, meta=meta, wire=wire)
    first = prepare(staged, submitted)

    assert first["render_destination"] == "device"
    assert first["sections"]["caption_meta"] is True
    assert original.assembly_plan == original_before
    assert editor_sections(staged, "guided_story")["text_elements"] == texts_before
    new_recipe = device_status(staged, "guided_story").request.recipe
    assert len(new_recipe.text_layers) == len(old_recipe.text_layers)
    assert new_recipe.audio == old_recipe.audio
    # Renderer IDs change at the ownership switch; editable row IDs above do not.
    assert [layer.model_dump(exclude={"id"}) for layer in new_recipe.text_layers] == [
        layer.model_dump(exclude={"id"}) for layer in old_recipe.text_layers
    ]
    reopened = editor_sections(staged, "guided_story")
    assert reopened["caption_meta"] == meta
    saved_before_retry = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as stale_retry:
        prepare(staged, submitted)
    assert stale_retry.value.status_code == 409
    assert staged.assembly_plan == saved_before_retry

    second = prepare(staged, request(staged, meta=reopened["caption_meta"], wire=wire))
    assert second["sections"]["caption_meta"] is True
    assert editor_sections(staged, "guided_story")["text_elements"] == texts_before
    assert len(device_status(staged, "guided_story").request.recipe.text_layers) == len(
        old_recipe.text_layers
    )


@pytest.mark.parametrize("consumer", ["cues", "text", "speech", "narration", "voiceover"])
def test_empty_caption_allowance_never_admits_existing_consumers(monkeypatch, consumer):
    _, staged = project(monkeypatch)
    variant = gj._find_variant(staged, "guided_story")
    cue = {"id": "cue", "text": "Spoken", "start_s": 0, "end_s": 1}
    if consumer == "cues":
        variant["caption_cues"] = [cue]
    elif consumer == "text":
        variant["text_elements"][0]["source_params"] = {"source": "caption_cue"}
    elif consumer == "speech":
        variant["speech_timing_basis"] = {"caption_cues": []}
    elif consumer == "narration":
        staged.assembly_plan["guided_story_execution_plan"]["narration"] = {"text": "Spoken"}
    else:
        staged.assembly_plan["voiceover_path"] = "fixture/voiceover.wav"
    before = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as error:
        prepare(staged, request(staged))
    assert error.value.status_code == 422
    assert error.value.detail == gj.CAPTION_TAB_COPY + "."
    assert staged.assembly_plan == before


def test_proposed_caption_text_does_not_gain_empty_lane_eligibility(monkeypatch):
    _, staged = project(monkeypatch)
    payload = request(staged)
    payload.text_elements[0]["source_params"] = {"source": "caption_cue"}
    before = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as error:
        prepare(staged, payload)
    assert error.value.detail == gj.CAPTION_TAB_COPY + "."
    assert staged.assembly_plan == before


def test_explicit_caption_cue_write_still_refuses(monkeypatch):
    _, staged = project(monkeypatch)
    with pytest.raises(HTTPException) as error:
        prepare(staged, request(staged, caption_cues=[]))
    assert error.value.detail == gj.CAPTION_TAB_COPY + "."


def test_stale_empty_caption_save_preserves_original(monkeypatch):
    _, staged = project(monkeypatch)
    payload = request(staged).model_copy(update={"base_generation": "stale"})
    before = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as error:
        prepare(staged, payload)
    assert error.value.status_code == 409
    assert staged.assembly_plan == before


@pytest.mark.parametrize(
    "field,value", [("resolved_archetype", "montage"), ("render_destination", "cloud")]
)
def test_empty_caption_allowance_is_limited_to_authored_guided_device_edits(
    monkeypatch, field, value
):
    _, staged = project(monkeypatch)
    gj._find_variant(staged, "guided_story")[field] = value
    before = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as error:
        prepare(staged, request(staged))
    assert error.value.status_code == 422
    assert staged.assembly_plan == before


def test_empty_caption_font_validation_still_precedes_mutation(monkeypatch):
    _, staged = project(monkeypatch)
    before = copy.deepcopy(staged.assembly_plan)
    with pytest.raises(HTTPException) as error:
        prepare(staged, request(staged, meta={"font": "not-a-real-font", "font_set": True}))
    assert error.value.status_code == 422
    assert error.value.detail == "Unknown caption font."
    assert staged.assembly_plan == before


@pytest.mark.parametrize("protocol", [24, 25])
def test_authored_save_keeps_caption_metadata_reader_floor(monkeypatch, protocol):
    _, staged = project(monkeypatch)
    payload = request(staged, meta={"appearance": {"line_overrides": {"line": {"size_px": 84}}}})
    before = copy.deepcopy(staged.assembly_plan)
    set_client_protocol(protocol)
    try:
        if protocol < 25:
            with pytest.raises(HTTPException) as error:
                prepare(staged, payload)
            assert error.value.status_code == 426
            assert staged.assembly_plan == before
        else:
            assert prepare(staged, payload)["sections"]["caption_meta"] is True
    finally:
        clear_request_context()
