"""KRI-1117: the Talk-to-camera speaker background over the HTTP editor surface.

Poll and Save answer an old app build with 426, the editor capability is advertised only
where the phone can render it, and Save sets or clears the lane.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledBackground
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    project_phone_subtitled_editor_sections,
)
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    _binding,
    _photo_visual,
    _pool_video_visual,
)
from tests.routes.test_phone_subtitled_editor_commit import (
    _enable_subtitled_editor,
    _resave,
    save,
)

PHOTO = _photo_visual()
VIDEO = _pool_video_visual()


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open_gate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*dict.fromkeys([*settings.phone_render_verified_features, "speakerBackground"])],
    )


def _background(visual=PHOTO, **changes) -> SubtitledBackground:
    return SubtitledBackground(
        **{
            "media_id": visual.media_id,
            "gcs_path": visual.gcs_path,
            "generation": visual.generation,
            "kind": visual.kind,
        }
        | changes
    )


def background_job(monkeypatch, *, background: SubtitledBackground | None = None, pinned=(PHOTO,)):
    """A phone Talking variant whose pinned recipe carries ``background`` (or none)."""
    _enable_subtitled_editor(monkeypatch)
    _open_gate(monkeypatch)
    bindings = (_binding(duration_s=10.0),)
    lanes = PhoneSubtitledLanes(background=background) if background is not None else None
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=_CUES, visuals=tuple(pinned), lanes=lanes
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [v.model_dump(mode="json") for v in pinned],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


# --- S16: an old build is told to update, never silently stripped -----------------------------


@pytest.mark.asyncio
async def test_device_render_poll_tells_an_old_build_to_update(monkeypatch) -> None:
    from app.routes import device_render

    job = background_job(monkeypatch, background=_background())
    assert _recipe(job).speaker_background is not None
    request = SimpleNamespace(recipe=_recipe(job), identity=SimpleNamespace(), recovery=None)
    monkeypatch.setattr(device_render, "_owned_job", AsyncMock(return_value=job))
    monkeypatch.setattr(
        device_render, "device_status", lambda *_a: SimpleNamespace(request=request)
    )
    monkeypatch.setattr(
        device_render, "_record", lambda *_a: ({}, SimpleNamespace(request=request, phase="x"))
    )
    set_client_protocol(26)
    with pytest.raises(HTTPException) as refused:
        await device_render.get_device_render(
            job.id, SimpleNamespace(id=uuid.uuid4()), variant_id="subtitled", db=AsyncMock()
        )
    assert refused.value.status_code == 426
    assert refused.value.detail["code"] == "client_update_required"
    assert refused.value.detail["feature"] == "speaker_background"
    assert refused.value.detail["minimum_client_protocol"] == 27


def test_update_detail_is_silent_for_a_recipe_without_a_background_and_for_a_new_build(
    monkeypatch,
) -> None:
    from app.routes import device_render

    plain = background_job(monkeypatch)
    asked = background_job(monkeypatch, background=_background())
    record: dict = {}

    def detail(job, protocol):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(job)))
        return device_render._client_update_detail(job, record, status)

    assert detail(plain, 26) is None
    assert detail(asked, 27) is None
    assert detail(asked, 26)["feature"] == "speaker_background"


def test_save_from_old_build_is_426(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    before = device_status(job, "subtitled").request.identity.recipe_revision
    set_client_protocol(26)
    with pytest.raises(HTTPException) as refused:
        save(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "speaker_background"
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background is not None


def test_an_old_build_saving_an_edit_that_never_had_one_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch)
    set_client_protocol(26)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is None


# --- the Save: set, clear, carry --------------------------------------------------------------


def _document(visual=PHOTO, **changes) -> dict:
    return {"media_id": visual.media_id, "kind": visual.kind, "source_start_s": 0.0} | changes


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def test_save_from_a_new_build_keeps_the_background(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(27)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None
    assert _variant(job)["speaker_background"] == _document()


def test_setting_a_pinned_video_pins_it_in_the_recipe_and_projects_it(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, VIDEO))
    set_client_protocol(27)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    prep = save(job, speaker_background=_document(VIDEO, source_start_s=1.5))

    assert prep["sections"]["speaker_background"] is True
    assert prep["render_task_id"] is None
    assert device_status(job, "subtitled").request.identity.recipe_revision == before + 1
    layer = _recipe(job).speaker_background
    assert (layer.media_kind, layer.source_start, layer.loop) == ("video", 1.5, True)
    assert "speakerBackground" in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"] == _document(VIDEO, source_start_s=1.5)
    lanes = _variant(job)[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["lanes"]
    assert lanes["background"]["generation"] == VIDEO.generation
    assert "background" in _variant(job)["phone_lane_receipt"]["applied"]


def test_a_later_save_of_another_lane_leaves_the_background_alone(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, VIDEO))
    set_client_protocol(27)
    save(job, speaker_background=_document(VIDEO, source_start_s=2.0))
    _resave(job, sound_effects=[])
    assert _recipe(job).speaker_background.source_start == 2.0
    assert _variant(job)["speaker_background"]["source_start_s"] == 2.0


def test_an_explicit_null_clears_it_and_the_recipe_is_what_a_never_asked_edit_compiles(
    monkeypatch,
) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(27)
    save(job, speaker_background=None)
    assert _recipe(job).speaker_background is None
    assert "speakerBackground" not in _recipe(job).required_capabilities
    assert "speaker_background" not in _variant(job)
    plain = background_job(monkeypatch)
    save(plain, sound_effects=[])
    assert (
        _recipe(job).model_dump(mode="json")["assets"]
        == _recipe(plain).model_dump(mode="json")["assets"]
    )
    assert "speaker_background" not in _recipe(job).model_dump(mode="json")


def test_clearing_works_with_the_kill_switch_off_and_an_old_build_is_not_needed(
    monkeypatch,
) -> None:
    job = background_job(monkeypatch, background=_background())
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    save(job, speaker_background=None)
    assert _recipe(job).speaker_background is None


def test_clearing_when_there_is_nothing_to_clear_commits_nothing(monkeypatch) -> None:
    job = background_job(monkeypatch)
    prep = save(job, speaker_background=None)
    assert prep["has_render_section"] is False
    assert prep["sections"]["speaker_background"] is False


def test_an_echo_of_the_stored_background_commits_nothing(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(27)
    save(job, sound_effects=[])
    prep = _resave(job, speaker_background=_document())
    assert prep["has_render_section"] is False


def test_a_background_from_outside_the_items_pool_is_refused(monkeypatch) -> None:
    """S19: only a visual this job pinned from THIS item's pool can be the background."""
    job = background_job(monkeypatch, pinned=(PHOTO,))
    set_client_protocol(27)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    for media_id in (VIDEO.media_id, str(uuid.uuid4()), "../../other-users-photo"):
        with pytest.raises(HTTPException) as refused:
            save(job, speaker_background={"media_id": media_id, "kind": "image"})
        assert refused.value.status_code == 422
        assert refused.value.detail == {"code": "speaker_background_media_not_in_pool"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background is None


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (_document(PHOTO, kind="video"), "speaker_background_kind_mismatch"),
        (_document(PHOTO, source_start_s=1.0), "speaker_background_start_out_of_range"),
        (
            _document(VIDEO, source_start_s=VIDEO.duration_s),
            "speaker_background_start_out_of_range",
        ),
    ],
)
def test_a_background_that_cannot_play_is_refused_with_its_reason(
    monkeypatch, document, code
) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, VIDEO))
    set_client_protocol(27)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=document)
    assert (refused.value.status_code, refused.value.detail["code"]) == (422, code)


def test_setting_needs_the_gate_and_a_new_enough_build(monkeypatch) -> None:
    job = background_job(monkeypatch)
    set_client_protocol(26)
    with pytest.raises(HTTPException) as old_build:
        save(job, speaker_background=_document())
    assert old_build.value.status_code == 426
    assert old_build.value.detail["feature"] == "speaker_background"

    set_client_protocol(27)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    with pytest.raises(HTTPException) as flag_off:
        save(job, speaker_background=_document())
    assert (flag_off.value.status_code, flag_off.value.detail["code"]) == (
        422,
        "speaker_background_disabled",
    )
    assert _recipe(job).speaker_background is None


def test_a_variant_that_cannot_carry_one_refuses_the_section(monkeypatch) -> None:
    job = background_job(monkeypatch)
    set_client_protocol(27)
    _variant(job)["render_destination"] = "cloud"
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_document())
    assert refused.value.detail == {"code": "speaker_background_unsupported"}


# --- capability: present only where the phone can render it ------------------------------------


def _capability_variant(destination="device", archetype="subtitled", **extra) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": archetype,
        "render_destination": destination,
        **extra,
    }


@pytest.mark.parametrize(
    ("destination", "archetype", "protocol", "expected"),
    [
        ("device", "subtitled", 27, True),
        ("device", "subtitled", 26, False),
        ("device", "subtitled", None, False),
        ("cloud", "subtitled", 27, False),
        ("device", "guided_story", 27, False),
        ("device", "narrated", 27, False),
    ],
)
def test_only_a_device_talk_to_camera_variant_advertises_it(
    monkeypatch, destination, archetype, protocol, expected
) -> None:
    job = background_job(monkeypatch)
    set_client_protocol(protocol)
    variant = _capability_variant(destination, archetype)
    assert gj._speaker_background_capability(job, variant) is expected


def test_the_capability_key_is_in_the_map_only_while_the_gate_is_open(monkeypatch) -> None:
    job = background_job(monkeypatch)
    variant = _variant(job)
    set_client_protocol(27)
    assert gj._editor_capabilities(job, variant)["speaker_background"] is True
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    assert "speaker_background" not in gj._editor_capabilities(job, variant)
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    set_client_protocol(26)
    assert "speaker_background" not in gj._editor_capabilities(job, variant)


def test_a_cloud_variant_and_an_authored_draft_never_advertise_it(monkeypatch) -> None:
    job = background_job(monkeypatch)
    set_client_protocol(27)
    for extra in ({"render_destination": "cloud"}, {"editor_timeline_mode": "authored"}):
        assert "speaker_background" not in gj._editor_capabilities(job, {**_variant(job), **extra})


def test_the_background_opens_the_editor_lanes_even_without_sfx_or_overlays(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(27)
    monkeypatch.setattr(settings, "sound_effects_enabled", False)
    monkeypatch.setattr(settings, "media_overlays_enabled", False)
    assert gj._phone_subtitled_editor_lanes_open(job, _variant(job)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "verified", "protocol", "expected"),
    [
        (True, True, None, False),
        (True, True, 26, False),
        (True, True, 27, True),
        (False, True, 27, False),
        (True, False, 27, False),
    ],
)
async def test_creation_capabilities_speaker_background(
    monkeypatch, flag, verified, protocol, expected
) -> None:
    from app.routes.creation_threads import CreationCapabilitiesOut, capabilities

    monkeypatch.setattr(settings, "speaker_background_enabled", flag)
    features = [f for f in settings.phone_render_verified_features if f != "speakerBackground"]
    if verified:
        features.append("speakerBackground")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    caps = await capabilities(SimpleNamespace(id=uuid.uuid4()), False, protocol)
    assert caps["speaker_background"] is expected
    assert CreationCapabilitiesOut.model_fields["speaker_background"].default is False


# --- projection: the editor document's `speaker_background` -------------------------------------


def test_the_pinned_recipe_projects_the_background_for_the_editor(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, VIDEO), background=_background(VIDEO))
    set_client_protocol(27)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == _document(VIDEO)
    shown = gj._augment_variant_with_phone_subtitled_editor_sections(job, _variant(job))
    assert shown["speaker_background"] == _document(VIDEO)


def test_an_edit_without_a_background_projects_nothing_new(monkeypatch) -> None:
    job = background_job(monkeypatch)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert "speaker_background" not in projected
    shown = gj._augment_variant_with_phone_subtitled_editor_sections(job, _variant(job))
    assert "speaker_background" not in shown


def test_a_saved_background_survives_a_reprojection_from_the_variant(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, VIDEO))
    set_client_protocol(27)
    save(job, speaker_background=_document(VIDEO, source_start_s=1.0))
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == _document(VIDEO, source_start_s=1.0)
