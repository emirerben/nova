"""KRI-1264: zoom and animation on a phone Talking edit's picture card, end to end on the server.

The editor writes ``media_overlays[i].editor_style`` (zoom, animation, and the KRI-1141 angle).
Save keeps zoom and animation on ``SubtitledOverlayCard``, the compile writes them as the card
clip's ``visual_placement.editor_style`` (the angle stays on ``transform.rotation_degrees``), and
the pinned recipe reads back into the editor as the same ``editor_style``. Before this Save refused
both and the compile had nowhere to put them. Rendering is `PlacedCardStyleTests` on the device.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledOverlayCard
from app.routes import device_render
from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    CARD_STYLE_PROTOCOL,
    CARD_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from app.services.kria_editor_ops import compile_editor_ops
from app.services.phone_rollout import card_style_supported, recipe_has_styled_card
from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections
from tests.routes.test_phone_chat_overlay_edit import _chat, _commit, _rendered_job
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


def _open_style(monkeypatch, *, protocol=CARD_STYLE_PROTOCOL, verified=True, tilt=False):
    tokens = [*gj.settings.phone_render_verified_features]
    if verified:
        tokens.append("cardStyle")
    if tilt:
        monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)
        tokens.append("clipTilt")
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", tokens)
    set_client_protocol(protocol)


def _close_style(monkeypatch):
    tokens = [t for t in gj.settings.phone_render_verified_features if t != "cardStyle"]
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", tokens)


_POP = {"version": 1, "entrance": "pop", "exit": "none", "loop": "none", "speed": 1}
_STILL = {"version": 1, "entrance": "none", "exit": "none", "loop": "none", "speed": 1}


def _style(*, zoom=1.0, animation=None, rotation=0.0, **changes) -> dict:
    return {
        "version": 1,
        "rotation_deg": rotation,
        "fit_mode": "cover",
        "zoom": zoom,
        "animation": animation or _STILL,
    } | changes


def _card_clip(job):
    request = device_status(job, "subtitled").request
    track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    return request.recipe, track.clips[0]


def _row(job) -> dict:
    return job.assembly_plan["variants"][0]["media_overlays"][0]


def _projected_style(job, variant=None):
    variant = variant or job.assembly_plan["variants"][0]
    sections = project_phone_subtitled_editor_sections(job.assembly_plan, variant)
    return sections["media_overlays"][0].get("editor_style")


def test_a_saved_zoom_and_pop_reach_the_recipe_and_read_back_into_the_editor(monkeypatch):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)

    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=2, animation=_POP))])

    recipe, clip = _card_clip(job)
    style = clip.visual_placement.editor_style
    assert (style.zoom, style.animation.entrance, style.rotation_deg) == (2, "pop", 0)
    assert clip.transform.rotation_degrees == 0
    assert clip.visual_placement.width_fraction == 0.35, "zoom rides on the style, not the size"
    assert recipe_has_styled_card(recipe)
    assert _row(job)["editor_style"]["zoom"] == 2
    assert _row(job)["editor_style"]["animation"]["entrance"] == "pop"
    assert _projected_style(job) == _style(zoom=2, animation=_POP)


def test_the_style_survives_the_pinned_recipe_round_trip_without_persisted_lanes(monkeypatch):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=3, animation=_POP))])
    variant = job.assembly_plan["variants"][0]
    variant.pop("_phone_subtitled_editor_lanes_v1")

    assert _projected_style(job, variant) == _style(zoom=3, animation=_POP)


def test_a_popped_tilted_card_keeps_its_angle_on_the_transform_and_the_rest_on_the_placement(
    monkeypatch,
):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch, protocol=CARD_STYLE_PROTOCOL, tilt=True)

    style = _style(zoom=2, animation=_POP, rotation=30)
    save(job, media_overlays=[_overlay_payload(editor_style=style)])

    _, clip = _card_clip(job)
    assert clip.transform.rotation_degrees == 30
    assert clip.visual_placement.editor_style.rotation_deg == 0
    assert clip.visual_placement.editor_style.zoom == 2
    assert _projected_style(job) == style


@pytest.mark.parametrize("zoom", [1.5, 4])
def test_any_zoom_from_one_to_four_saves_and_compiles(monkeypatch, zoom):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)

    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=zoom))])

    assert _card_clip(job)[1].visual_placement.editor_style.zoom == zoom


@pytest.mark.parametrize("zoom", [0.5, 4.5, math.nan, "big"])
def test_a_zoom_outside_one_to_four_is_refused_not_clamped(monkeypatch, zoom):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=zoom))])

    assert error.value.status_code == 422
    assert _card_clip(job)[1].visual_placement.editor_style is None


def test_a_zoomed_card_at_the_right_edge_compiles_with_its_own_x(monkeypatch):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)

    save(job, media_overlays=[_overlay_payload(x_frac=0.95, editor_style=_style(zoom=2))])

    placement = _card_clip(job)[1].visual_placement
    assert placement.x_fraction == 0.95 and placement.editor_style.zoom == 2


def test_a_plain_card_compiles_to_the_bytes_it_always_had(monkeypatch):
    job = phone_job(monkeypatch)
    pinned = _card_clip(job)[1].model_dump(mode="json")
    _open_style(monkeypatch)

    save(job, media_overlays=[_overlay_payload()])

    clip = _card_clip(job)[1].model_dump(mode="json")
    assert "editor_style" not in clip["visual_placement"]
    assert clip == pinned
    assert not {"zoom", "animation", "editor_style"} & set(_row(job))
    assert _projected_style(job) is None
    card = SubtitledOverlayCard(
        id="c", media_id="m", gcs_path="p", generation="1", start_s=0, end_s=1
    )
    assert not {"zoom", "animation"} & set(card.model_dump(mode="json"))


def test_resending_a_styled_card_unchanged_pins_the_same_card(monkeypatch):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)
    style = _style(zoom=2, animation=_POP)
    save(job, media_overlays=[_overlay_payload(editor_style=style)])
    first = _card_clip(job)[1].model_dump_json()

    _resave(job, media_overlays=[_overlay_payload(editor_style=style)])
    assert _card_clip(job)[1].model_dump_json() == first
    _resave(job, sound_effects=[_sfx_payload(at_s=4.0)])
    assert _card_clip(job)[1].model_dump_json() == first


def test_zoom_one_and_no_animation_is_the_plain_card(monkeypatch):
    job = phone_job(monkeypatch)
    plain = _card_clip(job)[1].model_dump_json()
    _open_style(monkeypatch)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=2, animation=_POP))])

    _resave(job, media_overlays=[_overlay_payload(editor_style=_style())])

    assert _card_clip(job)[1].model_dump_json() == plain
    assert "editor_style" not in _row(job)


@pytest.mark.parametrize(
    "style", [_style(zoom=2), _style(animation=_POP), _style(animation=_STILL | {"loop": "pulse"})]
)
def test_a_full_screen_card_cannot_be_zoomed_or_animated(monkeypatch, style):
    job = phone_job(monkeypatch, fullscreen=True)
    _open_style(monkeypatch)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(display_mode="fullscreen", editor_style=style)])

    assert error.value.status_code == 422
    assert "can't be zoomed or animated" in str(error.value.detail)


def test_a_card_fit_is_still_refused(monkeypatch):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(editor_style=_style(fit_mode="contain"))])

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("verified", "protocol"), [(False, CARD_STYLE_PROTOCOL), (True, CARD_STYLE_PROTOCOL - 1)]
)
def test_a_new_style_is_refused_while_a_gate_is_closed(monkeypatch, verified, protocol):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch, verified=verified, protocol=protocol)

    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=2))])

    assert error.value.status_code == 422
    assert "zooming or animating a picture isn't available" in str(error.value.detail)


def test_a_stored_style_survives_a_save_from_an_older_build_and_after_the_token_is_removed(
    monkeypatch,
):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch)
    style = _style(zoom=2, animation=_POP)
    save(job, media_overlays=[_overlay_payload(editor_style=style)])

    set_client_protocol(CARD_TILT_PROTOCOL)
    _resave(job, media_overlays=[_overlay_payload(x_frac=0.7, editor_style=style)])
    assert _card_clip(job)[1].visual_placement.editor_style.zoom == 2

    _close_style(monkeypatch)
    _resave(job, media_overlays=[_overlay_payload(x_frac=0.8, editor_style=style)])
    assert _card_clip(job)[1].visual_placement.editor_style.zoom == 2
    with pytest.raises(HTTPException):
        _resave(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=3))])
    _resave(job, media_overlays=[_overlay_payload(x_frac=0.8)])
    assert _card_clip(job)[1].visual_placement.editor_style is None


@pytest.mark.parametrize(
    ("protocol", "refused"),
    [(CARD_TILT_PROTOCOL, True), (42, True), (43, True), (44, False), (45, False)],
)
def test_a_build_below_the_style_protocol_is_told_to_update_instead_of_failing_the_recipe(
    monkeypatch, protocol, refused
):
    job = phone_job(monkeypatch)
    _open_style(monkeypatch, tilt=True)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=2, rotation=30))])
    recipe, _ = _card_clip(job)
    plain_recipe, _ = _card_clip(phone_job(monkeypatch))

    def detail(document):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=document))
        return device_render._client_update_detail(SimpleNamespace(assembly_plan={}), {}, status)

    assert detail(plain_recipe) is None
    assert detail(recipe) == (
        {"code": "client_update_required", "feature": "card_style", "minimum_client_protocol": 44}
        if refused
        else None
    )


def test_the_style_gate_is_dark_by_default_and_pinned_to_protocol_44():
    assert Settings.model_fields["media_card_style_min_client_protocol"].default == 44
    assert CARD_STYLE_PROTOCOL == 44
    assert "cardStyle" not in Settings.model_fields["phone_render_verified_features"].get_default(
        call_default_factory=True
    )


def test_the_capability_needs_the_token_the_build_and_an_open_card_lane(monkeypatch):
    job = phone_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    open_lane = {"overlays": True}

    _open_style(monkeypatch)
    assert gj._media_overlay_style_capability(variant, open_lane) is True
    assert gj._media_overlay_style_capability(variant, {"overlays": False}) is False
    cloud = {**variant, "render_destination": "cloud"}
    assert gj._media_overlay_style_capability(cloud, open_lane) is False
    montage = {**variant, "resolved_archetype": "montage"}
    assert gj._media_overlay_style_capability(montage, open_lane) is False
    set_client_protocol(CARD_STYLE_PROTOCOL - 1)
    assert gj._media_overlay_style_capability(variant, open_lane) is False
    set_client_protocol(CARD_STYLE_PROTOCOL)
    _close_style(monkeypatch)
    assert card_style_supported() is False
    assert gj._media_overlay_style_capability(variant, open_lane) is False


def test_the_editor_map_carries_the_capability_only_while_it_is_open(monkeypatch):
    job = _rendered_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    set_client_protocol(CARD_STYLE_PROTOCOL)
    closed = gj._editor_capabilities(job, variant)
    assert "media_overlay_style" not in closed
    assert closed["visual_editor_style"] is False

    _open_style(monkeypatch)
    opened = gj._editor_capabilities(job, variant)
    assert opened["media_overlay_style"] is True
    assert opened["visual_editor_style"] is False, "fit and full-screen styles stay closed"
    assert {k: v for k, v in opened.items() if k != "media_overlay_style"} == closed


def test_a_chat_tilt_and_straighten_keep_the_card_zoom_and_animation(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_style(monkeypatch, tilt=True)
    save(job, media_overlays=[_overlay_payload(editor_style=_style(zoom=2, animation=_POP))])

    for patch, angle in (({"rotation_deg": 12}, 12), ({"rotation_deg": 0}, 0)):
        base, _snapshot, ops = _chat(
            job, [{"op": "patch_overlay", "overlay_index": 0, "patch": patch}]
        )
        compiled = compile_editor_ops(job, base, ops)
        _commit(job, compiled)
        _, clip = _card_clip(job)
        assert clip.transform.rotation_degrees == angle
        assert clip.visual_placement.editor_style.zoom == 2
        assert clip.visual_placement.editor_style.animation.entrance == "pop"


_PARITY = json.loads(
    (Path(__file__).parents[5] / "tests/fixtures/card-style-parity/v1.json").read_text()
)


@pytest.mark.parametrize("case", _PARITY["cases"], ids=lambda case: case["name"])
def test_the_card_clip_the_compile_writes_is_the_one_the_device_parity_fixture_pins(case):
    """`PlacedCardStyleTests` decodes these same clips on the device side and renders them."""
    from app.agents._schemas.visual_editor import VisualAnimation
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from tests.pipeline.test_phone_subtitled_plan import (
        _CUES,
        _binding,
        _overlay_card,
        _photo_visual,
    )

    card = _overlay_card(
        rotation_deg=case["rotation_deg"],
        x_frac=case["x_frac"],
        zoom=case["zoom"],
        animation=VisualAnimation(entrance=case["entrance"]),
    )
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=_CUES,
        visuals=(_photo_visual(),),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )

    clip = next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0]
    assert clip.model_dump(mode="json") == case["clip"]


def test_a_video_card_takes_a_style_like_a_photo_card():
    from app.agents._schemas.visual_editor import VisualAnimation
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
                    zoom=2,
                    animation=VisualAnimation(entrance="pop"),
                )
            ]
        ),
    )

    clip = next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0]
    assert clip.visual_placement.editor_style.zoom == 2
    assert clip.visual_placement.editor_style.animation.entrance == "pop"
