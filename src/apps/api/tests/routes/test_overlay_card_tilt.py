"""KRI-1141: tilting a phone Talking edit's picture card, end to end on the server.

The editor writes ``media_overlays[i].editor_style.rotation_deg`` (clockwise on screen). Save keeps
it on ``SubtitledOverlayCard.rotation_deg``, the compile writes it as the card clip's
``transform.rotation_degrees`` (the same field a main-track tilt uses, so ``clipTilt`` is inferred),
and the pinned recipe reads back into the editor as the same ``editor_style``. Before this the
compile dropped the angle. Rendering is `PlacedCardTiltTests` on the device engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledOverlayCard
from app.routes import device_render
from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    CARD_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from app.services.phone_rollout import (
    card_tilt_supported,
    recipe_has_clip_tilt,
    recipe_has_tilted_card,
)
from app.services.phone_subtitled_editor import (
    project_phone_subtitled_editor_sections,
    sections_from_lanes,
)
from tests.routes.test_phone_subtitled_editor_commit import (
    _overlay_payload,
    _resave,
    _sfx_payload,
    phone_job,
    save,
)


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open_tilt(monkeypatch, *, protocol=CARD_TILT_PROTOCOL, enabled=True, verified=True):
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", enabled)
    if verified:
        monkeypatch.setattr(
            gj.settings,
            "phone_render_verified_features",
            [*gj.settings.phone_render_verified_features, "clipTilt"],
        )
    set_client_protocol(protocol)


def _style(rotation: float, **changes) -> dict:
    return {"version": 1, "rotation_deg": rotation, "fit_mode": "cover", "zoom": 1} | changes


def _card_clip(job):
    request = device_status(job, "subtitled").request
    track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    return request.recipe, track.clips[0]


def _row(job) -> dict:
    return job.assembly_plan["variants"][0]["media_overlays"][0]


def test_a_saved_angle_reaches_the_recipe_and_reads_back_into_the_editor(monkeypatch):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)

    save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])

    recipe, clip = _card_clip(job)
    assert clip.transform.rotation_degrees == 30
    assert clip.visual_placement.width_fraction is not None
    assert "clipTilt" in recipe.required_capabilities
    assert recipe_has_tilted_card(recipe) and recipe_has_clip_tilt(recipe)
    assert _row(job)["editor_style"]["rotation_deg"] == 30
    sections = project_phone_subtitled_editor_sections(
        job.assembly_plan, job.assembly_plan["variants"][0]
    )
    assert sections["media_overlays"][0]["editor_style"]["rotation_deg"] == 30


def test_the_angle_survives_the_pinned_recipe_round_trip_without_persisted_lanes(monkeypatch):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(-30))])
    variant = job.assembly_plan["variants"][0]
    variant.pop("_phone_subtitled_editor_lanes_v1")

    sections = project_phone_subtitled_editor_sections(job.assembly_plan, variant)

    assert sections["media_overlays"][0]["editor_style"]["rotation_deg"] == -30


@pytest.mark.parametrize(
    ("asked", "stored"),
    [(270, -90), (-190, 170), (180, 180), (-180, 180), (360, 0), (12.345, 12.35)],
)
def test_the_angle_is_wrapped_into_half_a_turn_either_way(monkeypatch, asked, stored):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)

    save(job, media_overlays=[_overlay_payload(editor_style=_style(asked))])

    _, clip = _card_clip(job)
    assert clip.transform.rotation_degrees == stored


def test_an_unrelated_save_keeps_the_angle(monkeypatch):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])

    _resave(job, sound_effects=[_sfx_payload(at_s=4.0)])

    assert _card_clip(job)[1].transform.rotation_degrees == 30


def test_a_card_resent_without_a_style_is_straightened_and_the_recipe_is_the_untilted_one(
    monkeypatch,
):
    job = phone_job(monkeypatch)
    plain_digest = device_status(job, "subtitled").request.recipe.model_dump_json()
    _open_tilt(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])

    _resave(job, media_overlays=[_overlay_payload()])

    recipe, clip = _card_clip(job)
    assert clip.transform.rotation_degrees == 0
    assert "clipTilt" not in recipe.required_capabilities
    assert "editor_style" not in _row(job)
    # Same card, same geometry as the untilted pin: only the revision/identity differ.
    assert clip.model_dump_json() == _card_clip_json(plain_digest)


def _card_clip_json(recipe_json: str) -> str:
    recipe = EditRecipeV2.model_validate_json(recipe_json)
    track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    return track.clips[0].model_dump_json()


def test_an_untilted_card_compiles_to_the_same_bytes_as_a_card_that_never_had_an_angle(monkeypatch):
    job = phone_job(monkeypatch)
    _, clip = _card_clip(job)
    dumped = clip.model_dump(mode="json")
    assert dumped["transform"] == {
        "scale": 1,
        "rotation_degrees": 0,
        "position_x": 0,
        "position_y": 0,
    }
    card = SubtitledOverlayCard(
        id="c", media_id="m", gcs_path="p", generation="1", start_s=0, end_s=1
    )
    assert "rotation_deg" not in card.model_dump(mode="json")
    assert (
        "editor_style"
        not in sections_from_lanes(PhoneSubtitledLanes(overlays=[card]), labels={})[
            "media_overlays"
        ][0]
    )


@pytest.mark.parametrize(
    "style",
    [
        _style(30, zoom=2),
        _style(30, fit_mode="contain"),
        _style(30, animation={"version": 1, "entrance": "pop"}),
        {"version": 1, "rotation_deg": 400},
        {"rotation_deg": "tilted"},
    ],
)
def test_a_card_style_with_anything_but_an_angle_is_refused_not_dropped(monkeypatch, style):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(editor_style=style)])

    assert error.value.status_code == 422
    assert _card_clip(job)[1].transform.rotation_degrees == 0


def test_a_full_screen_card_cannot_be_tilted(monkeypatch):
    job = phone_job(monkeypatch, fullscreen=True)
    _open_tilt(monkeypatch)

    with pytest.raises(HTTPException) as error:
        save(
            job,
            media_overlays=[_overlay_payload(display_mode="fullscreen", editor_style=_style(30))],
        )

    assert error.value.status_code == 422
    assert "can't be tilted" in str(error.value.detail)


def test_an_upright_style_on_a_full_screen_card_is_still_a_full_screen_card(monkeypatch):
    job = phone_job(monkeypatch, fullscreen=True)
    _open_tilt(monkeypatch)

    save(job, media_overlays=[_overlay_payload(display_mode="fullscreen", editor_style=_style(0))])

    track_ids = [t.id for t in device_status(job, "subtitled").request.recipe.tracks]
    assert "subtitled-fullscreen" in track_ids


@pytest.mark.parametrize(
    ("enabled", "verified", "protocol"),
    [(False, True, 30), (True, False, 30), (True, True, 29)],
)
def test_a_new_angle_is_refused_while_a_gate_is_closed(monkeypatch, enabled, verified, protocol):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch, enabled=enabled, verified=verified, protocol=protocol)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])

    assert error.value.status_code == 422
    assert "tilting a picture isn't available" in str(error.value.detail)


def test_a_stored_angle_survives_a_save_after_the_kill_switch_is_flipped(monkeypatch):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)

    _resave(job, media_overlays=[_overlay_payload(x_frac=0.8, editor_style=_style(30))])

    assert _card_clip(job)[1].transform.rotation_degrees == 30
    # Straightening is always allowed.
    _resave(job, media_overlays=[_overlay_payload(x_frac=0.8)])
    assert _card_clip(job)[1].transform.rotation_degrees == 0


@pytest.mark.parametrize(
    ("protocol", "refused"), [(27, True), (28, True), (29, True), (30, False), (31, False)]
)
def test_a_build_below_the_card_protocol_is_told_to_update_instead_of_drawing_the_card_upright(
    monkeypatch, protocol, refused
):
    job = phone_job(monkeypatch)
    _open_tilt(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(30))])
    recipe, _ = _card_clip(job)
    plain_job = phone_job(monkeypatch)
    plain_recipe, _ = _card_clip(plain_job)

    def detail(document):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=document))
        return device_render._client_update_detail(SimpleNamespace(assembly_plan={}), {}, status)

    assert detail(plain_recipe) is None
    assert detail(recipe) == (
        {
            "code": "client_update_required",
            "feature": "card_tilt",
            "minimum_client_protocol": 30,
        }
        if refused
        else None
    )


def test_the_card_gate_is_dark_by_default_and_pinned_to_protocol_30():
    assert Settings.model_fields["media_card_tilt_min_client_protocol"].default == 30
    assert CARD_TILT_PROTOCOL == 30
    assert Settings.model_fields["media_tilt_enabled"].default is False


def test_the_capability_needs_the_flag_the_token_the_build_and_an_open_card_lane(monkeypatch):
    job = phone_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    open_lane = {"overlays": True}

    _open_tilt(monkeypatch)
    assert gj._media_overlay_tilt_capability(variant, open_lane) is True
    assert gj._media_overlay_tilt_capability(variant, {"overlays": False}) is False
    assert (
        gj._media_overlay_tilt_capability({**variant, "render_destination": "cloud"}, open_lane)
        is False
    )
    assert (
        gj._media_overlay_tilt_capability({**variant, "resolved_archetype": "montage"}, open_lane)
        is False
    )
    set_client_protocol(CARD_TILT_PROTOCOL - 1)
    assert gj._media_overlay_tilt_capability(variant, open_lane) is False
    set_client_protocol(CARD_TILT_PROTOCOL)
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    assert gj._media_overlay_tilt_capability(variant, open_lane) is False
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", [])
    assert card_tilt_supported() is False
    assert gj._media_overlay_tilt_capability(variant, open_lane) is False


_PARITY = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/card-tilt-parity/v1.json").read_text()
)


@pytest.mark.parametrize("case", _PARITY["cases"], ids=lambda case: case["name"])
def test_the_card_clip_the_compile_writes_is_the_one_the_device_parity_fixture_pins(case):
    """`PlacedCardTiltTests` decodes these same clips on the device side and renders them."""
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from tests.pipeline.test_phone_subtitled_plan import (
        _CUES,
        _binding,
        _overlay_card,
        _photo_visual,
    )

    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=_CUES,
        visuals=(_photo_visual(),),
        lanes=PhoneSubtitledLanes(overlays=[_overlay_card(rotation_deg=case["rotation_deg"])]),
    )

    clip = next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0]
    assert clip.model_dump(mode="json") == case["clip"]
    assert ("clipTilt" in recipe.required_capabilities) is case["clip_tilt"]


def test_a_video_card_turns_like_a_photo_card():
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from tests.pipeline.test_phone_subtitled_plan import (
        VIDEO_ID,
        VIDEO_PATH,
        _binding,
        _overlay_card,
        _pool_video_visual,
    )

    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=[],
        visuals=(_pool_video_visual(duration_s=2.0),),
        lanes=PhoneSubtitledLanes(
            overlays=[
                _overlay_card(
                    media_id=VIDEO_ID,
                    gcs_path=VIDEO_PATH,
                    generation="88",
                    kind="video",
                    rotation_deg=-20,
                )
            ]
        ),
    )

    clip = next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0]
    assert clip.transform.rotation_degrees == -20
    assert clip.visual_placement.width_fraction is not None
    assert {"clipTilt", "visualVideos"} <= recipe.required_capabilities
