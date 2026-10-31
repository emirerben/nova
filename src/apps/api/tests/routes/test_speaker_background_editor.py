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
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    _binding,
    _photo_visual,
    _pool_video_visual,
)
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor, save

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
