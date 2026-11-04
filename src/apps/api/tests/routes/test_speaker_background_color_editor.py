"""KRI-1173: a solid-colour background fill over the HTTP editor surface.

Rows S49, S54, S55 and S56 of `docs/reviews/speaker-background-color-failure-modes.md`: poll and
Save answer a build below protocol 34 with 426, the editor capability is advertised only where the
phone can draw it, and Save sets, replaces or clears the colour without pinning anything.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.phone_speaker_background import background_pin_request, background_pin_requests
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    lanes_from_recipe,
    project_phone_subtitled_editor_sections,
    variant_carries_speaker_background_color,
)
from tests.pipeline.test_phone_subtitled_plan import _photo_visual, _pool_video_visual
from tests.routes.test_phone_subtitled_editor_commit import _resave, save
from tests.routes.test_speaker_background_editor import (
    _background,
    _capability_variant,
    _document,
    _recipe,
    _variant,
    background_job,
)

PHOTO = _photo_visual()
SECOND = _photo_visual(
    media_id="photo-2", gcs_path="users/owner/plan/item/pool/photo-2.jpg", generation="78"
)
VIDEO = _pool_video_visual()
POOL = (PHOTO, SECOND, VIDEO)
WHITE = {"kind": "color", "color": "#FFFFFF"}
CORNER = {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.80}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open_colour(monkeypatch, *, token: bool = True, windows: bool = False) -> None:
    ours = {"speakerBackgroundColor", "speakerBackgroundWindows"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if token:
        features.append("speakerBackgroundColor")
    if windows:
        features.append("speakerBackgroundWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def colour_job(monkeypatch, *, pinned=(PHOTO,)):
    """A phone Talking variant whose pinned recipe carries a white colour fill."""
    job = background_job(monkeypatch, pinned=pinned)
    _open_colour(monkeypatch)
    set_client_protocol(34)
    save(job, speaker_background=WHITE)
    clear_request_context()
    return job


# --- S49: an old build is told to update, never silently stripped -----------------------------


def test_poll_tells_an_old_build_to_update(monkeypatch) -> None:
    from app.routes import device_render

    job = colour_job(monkeypatch)
    assert _recipe(job).speaker_background.uses_color
    media = background_job(monkeypatch, background=_background())
    record: dict = {}

    def detail(target, protocol):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(target)))
        return device_render._client_update_detail(target, record, status)

    assert detail(job, 33) == {
        "code": "client_update_required",
        "feature": "speaker_background_color",
        "minimum_client_protocol": 34,
    }
    assert detail(job, 34) is None
    # A photo fill is still fine on 27..33, and still 426 (its own feature) below 27.
    assert detail(media, 33) is None
    assert detail(media, 26)["feature"] == "speaker_background"
    # Below the first cut's protocol its refusal wins for a colour recipe too.
    assert detail(job, 26)["feature"] == "speaker_background"


def test_save_from_old_build_is_426(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    set_client_protocol(33)
    with pytest.raises(HTTPException) as refused:
        _resave(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "speaker_background_color",
        "minimum_client_protocol": 34,
    }
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background.color == "#FFFFFF"


def test_an_old_build_saving_a_photo_fill_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(33)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None
    assert not variant_carries_speaker_background_color(job.assembly_plan, _variant(job))


def test_setting_a_colour_needs_a_new_enough_build_and_the_token(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_colour(monkeypatch)
    set_client_protocol(33)
    with pytest.raises(HTTPException) as old_build:
        save(job, speaker_background=WHITE)
    assert old_build.value.status_code == 426
    assert old_build.value.detail["feature"] == "speaker_background_color"
    assert old_build.value.detail["minimum_client_protocol"] == 34
    assert _recipe(job).speaker_background is None


# --- S55: a closed gate refuses a SET, never a clear -------------------------------------------


@pytest.mark.parametrize(
    ("close", "reason"),
    [
        ("token", "color_token_not_verified"),
    ],
)
def test_save_colour_while_closed_is_422(monkeypatch, close, reason) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_colour(monkeypatch, token=False)
    set_client_protocol(34)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=WHITE)
    assert dark.value.status_code == 422
    assert dark.value.detail == {"code": "speaker_background_color_disabled", "reason": reason}
    assert _recipe(job).speaker_background is None
    # A media Save on the same variant is unaffected by the colour token.
    save(job, speaker_background=_document(SECOND))
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


def test_the_base_kill_switch_wins_over_the_colour_gate(monkeypatch) -> None:
    job = background_job(monkeypatch)
    _open_colour(monkeypatch)
    set_client_protocol(34)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=WHITE)
    assert dark.value.detail["code"] == "speaker_background_disabled"


def test_clearing_a_colour_is_allowed_with_every_gate_closed(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    _open_colour(monkeypatch, token=False)
    set_client_protocol(34)
    _resave(job, speaker_background=None)
    assert _recipe(job).speaker_background is None
    assert "speaker_background" not in _variant(job)
    assert "speakerBackgroundColor" not in _recipe(job).required_capabilities


# --- S54: a colour is not a pool visual ---------------------------------------------------------


def test_save_a_colour_needs_no_pin(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=())
    _open_colour(monkeypatch)
    set_client_protocol(34)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    prep = save(job, speaker_background={"kind": "color", "color": "#00ff00"})

    assert prep["sections"]["speaker_background"] is True
    assert prep["render_task_id"] is None
    assert device_status(job, "subtitled").request.identity.recipe_revision == before + 1
    layer = _recipe(job).speaker_background
    assert (layer.media_kind, layer.color, layer.asset_id) == ("color", "#00FF00", None)
    assert {"speakerBackground", "speakerBackgroundColor"} <= _recipe(job).required_capabilities
    # Lowercase in, UPPERCASE stored (document, lane state and recipe).
    assert _variant(job)["speaker_background"] == {"kind": "color", "color": "#00FF00"}
    lane = _variant(job)[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["lanes"]["background"]
    assert lane == {"kind": "color", "color": "#00FF00", "source_start_s": 0.0}
    assert not {"media_id", "gcs_path", "generation"} & set(lane)
    assert "background" in _variant(job)["phone_lane_receipt"]["applied"]
    assert variant_carries_speaker_background_color(job.assembly_plan, _variant(job))
    # The route would pin nothing for it.
    payload = gj.EditorCommitRequest(base_generation="first", speaker_background=WHITE)
    assert background_pin_requests(job, "subtitled", payload) == []
    assert background_pin_request(job, "subtitled", payload) is None


def test_the_pin_still_covers_the_windows_over_a_colour(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=())
    _open_colour(monkeypatch, windows=True)
    set_client_protocol(34)
    window = {
        "media_id": SECOND.media_id,
        "kind": "image",
        "source_start_s": 0.0,
        "start_s": 0.0,
        "end_s": 3.0,
    }
    payload = gj.EditorCommitRequest(
        base_generation="first", speaker_background=WHITE | {"windows": [window]}
    )
    assert background_pin_requests(job, "subtitled", payload) == [(SECOND.media_id, "image")]


@pytest.mark.parametrize(
    "section",
    [
        WHITE | {"media_id": PHOTO.media_id},
        {"kind": "color"},
        {"kind": "color", "color": "white"},
        {"kind": "color", "color": "#FFF"},
        {"kind": "color", "color": "#FFFFFF80"},
        {"kind": "color", "color": "FFFFFF"},
        {"kind": "color", "color": ""},
        WHITE | {"source_start_s": 2.0},
        {"kind": "image"},
        {"kind": "image", "media_id": PHOTO.media_id, "color": "#FFFFFF"},
    ],
)
def test_colour_with_a_media_id_is_refused(monkeypatch, section) -> None:
    """A colour with a media id, a colour that is not `#RRGGBB`, a media kind without an id."""
    job = background_job(monkeypatch, pinned=POOL)
    _open_colour(monkeypatch)
    set_client_protocol(34)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=section)
    assert refused.value.status_code == 422
    assert refused.value.detail == {"code": "speaker_background_color_invalid"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background is None


# --- set, replace, carry -----------------------------------------------------------------------


def test_a_colour_replaces_a_photo_fill_and_a_photo_replaces_a_colour(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(), pinned=POOL)
    _open_colour(monkeypatch)
    set_client_protocol(34)
    save(job, speaker_background=WHITE)
    assert _recipe(job).speaker_background.uses_color
    assert "stillImages" not in _recipe(job).speaker_background.model_dump(mode="json")
    _resave(job, speaker_background=_document(SECOND))
    layer = _recipe(job).speaker_background
    assert not layer.uses_color and layer.asset_id.endswith(SECOND.media_id)
    assert "speakerBackgroundColor" not in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"] == _document(SECOND)


def test_a_later_save_of_another_lane_keeps_the_colour(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    set_client_protocol(34)
    _resave(job, sound_effects=[])
    assert _recipe(job).speaker_background.color == "#FFFFFF"
    assert _variant(job)["speaker_background"] == WHITE


def test_an_echo_of_the_stored_colour_commits_nothing(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    set_client_protocol(34)
    prep = _resave(job, speaker_background={"kind": "color", "color": "#ffffff"})
    assert prep["has_render_section"] is False


def test_a_colour_with_windows_and_a_speaker_frame_saves(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_colour(monkeypatch, windows=True)
    set_client_protocol(34)
    windows = [
        {
            "media_id": SECOND.media_id,
            "kind": "image",
            "source_start_s": 0.0,
            "start_s": 0.0,
            "end_s": 3.0,
        },
        {
            "media_id": VIDEO.media_id,
            "kind": "video",
            "source_start_s": 1.0,
            "start_s": 3.0,
            "end_s": 6.0,
        },
    ]
    document = WHITE | {"windows": windows, "speaker_frame": CORNER}
    save(job, speaker_background=document)
    layer = _recipe(job).speaker_background
    assert layer.color == "#FFFFFF" and len(layer.windows) == 2
    assert layer.speaker_frame.model_dump() == CORNER
    assert {"speakerBackgroundColor", "speakerBackgroundWindows"} <= (
        _recipe(job).required_capabilities
    )
    assert _variant(job)["speaker_background"] == document
    # The persisted lane state survives the next Save of another lane.
    set_client_protocol(34)
    _resave(job, sound_effects=[])
    assert _variant(job)["speaker_background"] == document
    assert len(_recipe(job).speaker_background.windows) == 2


# --- capability: present only where the phone can draw it ----------------------------------------


@pytest.mark.parametrize(
    ("destination", "archetype", "protocol", "token", "expected"),
    [
        ("device", "subtitled", 34, True, True),
        ("device", "subtitled", 33, True, False),
        ("device", "subtitled", None, True, False),
        ("device", "subtitled", 34, False, False),
        ("cloud", "subtitled", 34, True, False),
        ("device", "guided_story", 34, True, False),
    ],
)
def test_only_a_device_talk_to_camera_variant_advertises_the_colour(
    monkeypatch, destination, archetype, protocol, token, expected
) -> None:
    job = background_job(monkeypatch)
    _open_colour(monkeypatch, token=token)
    set_client_protocol(protocol)
    variant = _capability_variant(destination, archetype)
    assert gj._speaker_background_color_capability(job, variant) is expected


def test_the_colour_key_is_in_the_map_only_while_its_gate_is_open(monkeypatch) -> None:
    job = background_job(monkeypatch)
    variant = _variant(job)
    _open_colour(monkeypatch)
    set_client_protocol(34)
    capabilities = gj._editor_capabilities(job, variant)
    assert capabilities["speaker_background"] is True
    assert capabilities["speaker_background_color"] is True
    set_client_protocol(33)  # a build that knows backgrounds but not colours
    capabilities = gj._editor_capabilities(job, variant)
    assert capabilities["speaker_background"] is True
    assert "speaker_background_color" not in capabilities
    set_client_protocol(34)
    _open_colour(monkeypatch, token=False)
    assert "speaker_background_color" not in gj._editor_capabilities(job, variant)
    _open_colour(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    capabilities = gj._editor_capabilities(job, variant)
    assert "speaker_background" not in capabilities
    assert "speaker_background_color" not in capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "base", "token", "protocol", "expected"),
    [
        (True, True, True, None, False),
        (True, True, True, 33, False),
        (True, True, True, 34, True),
        (False, True, True, 34, False),
        (True, False, True, 34, False),
        (True, True, False, 34, False),
    ],
)
async def test_creation_capabilities_speaker_background_color(
    monkeypatch, flag, base, token, protocol, expected
) -> None:
    import uuid

    from app.routes.creation_threads import CreationCapabilitiesOut, capabilities

    monkeypatch.setattr(settings, "speaker_background_enabled", flag)
    ours = {"speakerBackground", "speakerBackgroundColor"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if base:
        features.append("speakerBackground")
    if token:
        features.append("speakerBackgroundColor")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    caps = await capabilities(SimpleNamespace(id=uuid.uuid4()), False, protocol)
    assert caps["speaker_background_color"] is expected
    assert CreationCapabilitiesOut.model_fields["speaker_background_color"].default is False


# --- S56: the projection carries the colour -----------------------------------------------------


def test_the_pinned_recipe_projects_the_colour_for_the_editor(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    set_client_protocol(34)
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == WHITE
    shown = gj._augment_variant_with_phone_subtitled_editor_sections(job, _variant(job))
    assert shown["speaker_background"] == WHITE


def test_a_saved_colour_survives_a_reprojection_from_the_variant(monkeypatch) -> None:
    job = colour_job(monkeypatch)
    _variant(job).pop("speaker_background")
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == WHITE
    rebuilt = lanes_from_recipe(_recipe(job), visuals=())
    assert rebuilt.background.color == "#FFFFFF"
