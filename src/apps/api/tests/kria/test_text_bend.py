"""KRI-646: text Bend (curved / rainbow text) -- the server half.

The iPhone draws the curve; the server only STORES `bend`, PASSES IT THROUGH into the
phone recipe's text layers and GATES old app builds. These are contract / guard tests.

Failure modes (listed before the code was written):
 B1  `bend` is dropped on save (the text models are `extra="ignore"`)
 B2  an out-of-range or non-finite bend reaches the wire (the recipe model rejects it)
 B3  a layer whose effect cannot follow a curve gets a bend anyway (half-applied)
 B4  a straight layer's wire shape changes (every pinned recipe / Swift fixture drifts)
 B5  an old app build is handed a recipe it cannot decode (it would FAIL the job)
 B6  the capability is advertised to a build / variant that cannot draw it
 B7  receipts, copy-style or a slide save lose the value
"""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routes.generative_jobs as gj
from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.portable_text import BEND_SUPPORTED_EFFECTS, PortableTextLayer
from app.pipeline.canvas import Canvas
from app.pipeline.generative_overlays import build_intro_overlay, build_overlays_from_text_elements
from app.pipeline.portable_text_layout import _bend_supported, compile_text_overlay
from app.schemas.slide_post import SlideEdits, SlideTextElement
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    text_bend_client_supported,
    text_bend_supported,
    validate_phone_pilot_recipe,
)

CANVAS = Canvas(1080, 1920)


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _verify_bend(monkeypatch) -> None:
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "textBend"],
    )


# --- (a) the stored models keep bend and clamp it -----------------------------------------


def _element(**changes) -> dict:
    return {"text": "Hello", "start_s": 0.0, "end_s": 2.0, "role": "generative_intro", **changes}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(40, 40.0), (-65.5, -65.5), (1000, 100.0), (-1000, -100.0), (0, 0.0), (None, None)],
)
def test_text_element_keeps_and_clamps_bend(raw, expected) -> None:
    element = TextElement.model_validate(_element(bend=raw))
    assert element.bend == expected
    assert TextElement.model_validate(element.model_dump(mode="json")).bend == expected


@pytest.mark.parametrize("raw", ["nope", float("nan"), float("inf")])
def test_text_element_treats_junk_bend_as_straight(raw) -> None:
    assert TextElement.model_validate(_element(bend=raw)).bend is None


def test_bend_survives_the_legacy_burn_dict_adapter() -> None:
    from app.agents._schemas.text_element import _burn_dict_to_text_element

    element = _burn_dict_to_text_element(
        {"text": "Hello", "start_s": 0.0, "end_s": 2.0, "bend": 30}
    )
    assert element is not None and element.bend == 30.0


@pytest.mark.parametrize(("raw", "expected"), [(250, 100.0), (-250, -100.0), (12.5, 12.5)])
def test_slide_text_keeps_and_clamps_bend(raw, expected) -> None:
    text = SlideTextElement(id="t1", text="Lisbon", bend=raw)
    assert text.bend == expected
    edits = SlideEdits(texts=[text])
    again = SlideEdits.model_validate(edits.model_dump(mode="json"))
    assert again.texts[0].bend == expected


def test_a_slide_text_without_bend_keeps_its_old_serialized_form() -> None:
    dumped = SlideTextElement(id="t1", text="Lisbon").model_dump(mode="json")
    assert "bend" not in dumped
    assert "bend" in SlideTextElement(id="t1", text="Lisbon", bend=10).model_dump(mode="json")


# --- (b) compile: bend reaches the layer only where the effect can follow a curve ---------


def _overlay(effect: str, *, bend: float | None = 60.0, **extra) -> dict:
    overlay = build_intro_overlay(
        "Hello bendy world",
        effect=effect,
        start_s=0.0,
        end_s=3.0,
        text_size_px=64,
        bend=bend,
    )
    assert overlay is not None
    # `build_intro_overlay` coerces effects it does not know (none, lyric-line) to static.
    overlay["effect"] = effect
    overlay.update(extra)
    return overlay


def _compile(overlay: dict) -> PortableTextLayer:
    return compile_text_overlay(overlay, layer_id="bend", canvas=CANVAS, dissolve_seed=7)[0]


SUPPORTED_EFFECTS = ["static", "fade-in", "pop-in", "scale-up", "slide-up", "slide-down", "bounce"]
UNSUPPORTED_EFFECTS = [
    "ink-reveal",
    "typewriter",
    "stream-in",
    "smooth-type",
    "staggered-slice",
    "handwriting",
    "karaoke-line",
    "dissolve-out",
]


@pytest.mark.parametrize("effect", SUPPORTED_EFFECTS)
def test_a_supported_effect_carries_the_bend(effect) -> None:
    layer = _compile(_overlay(effect))
    assert layer.bend == 60.0
    assert layer.model_dump(mode="json")["bend"] == 60.0


def test_none_effect_and_slide_in_carry_the_bend() -> None:
    for effect in ("none", "slide-in"):
        assert _compile(_overlay(effect)).bend == 60.0


@pytest.mark.parametrize("effect", UNSUPPORTED_EFFECTS)
def test_an_unsupported_effect_stays_straight(effect) -> None:
    layer = _compile(_overlay(effect))
    assert layer.bend is None
    assert "bend" not in layer.model_dump(mode="json")


def test_lyric_line_phase_typewriter_and_giant_title_stay_straight() -> None:
    assert _compile(_overlay("lyric-line")).bend is None
    typed = _overlay("static", animation_phases={"entrance": "typewriter", "exit": "none"})
    assert _compile(typed).bend is None
    popped = _overlay("static", animation_phases={"entrance": "pop", "exit": "fade"})
    assert _compile(popped).bend == 60.0
    giant = _overlay("static", theme_transition={"type": "giant-title-wipe"})
    assert _compile(giant).bend is None


def test_bend_supported_is_one_rule_over_the_documented_effect_set() -> None:
    assert BEND_SUPPORTED_EFFECTS == {
        "static",
        "none",
        "fade-in",
        "pop-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "slide-in",
        "bounce",
        "caption-pop",
    }
    assert _bend_supported("caption-pop", None)
    assert not _bend_supported("static", None, giant_title=True)
    assert not _bend_supported("static", {"entrance": "none", "exit": "typewriter"})
    for effect in (*UNSUPPORTED_EFFECTS, "lyric-line"):
        assert not _bend_supported(effect, None)


@pytest.mark.parametrize("bend", [None, 0, 0.0])
def test_a_zero_bend_leaves_the_wire_shape_untouched(bend) -> None:
    straight = _compile(_overlay("static", bend=None))
    layer = _compile(_overlay("static", bend=bend))
    assert layer.bend is None
    assert layer.model_dump(mode="json") == straight.model_dump(mode="json")
    assert "bend" not in layer.model_dump(mode="json")
    assert "bend" not in _overlay("static", bend=bend)


def test_bend_is_clamped_at_the_overlay_boundary() -> None:
    assert _compile(_overlay("static", bend=500)).bend == 100.0
    assert _compile(_overlay("static", bend=-500)).bend == -100.0
    assert _compile(_overlay("static", bend=float("nan"))).bend is None


def test_text_elements_flow_into_the_overlay_dict_only_when_bent() -> None:
    rows = [
        TextElement.model_validate(_element(bend=25)),
        TextElement.model_validate(_element(text="Straight")),
    ]
    bent, straight = build_overlays_from_text_elements(rows, video_duration_s=5.0)
    assert bent["bend"] == 25.0
    assert "bend" not in straight


# --- every phone plan writer that compiles authored text --------------------------------


def _bent_row(**changes) -> dict:
    return _element(bend=45, size_px=64, effect="static", **changes)


def test_subtitled_text_lane_stamps_bend() -> None:
    from app.pipeline.phone_subtitled_plan import _compile_text_lane

    bent, plain = _compile_text_lane(
        [_bent_row(), _element(text="Plain", start_s=2.0, end_s=4.0)],
        user_edited=True,
        timeline_duration_s=10.0,
    )
    assert bent.bend == 45.0
    assert plain.bend is None
    assert "bend" not in plain.model_dump(mode="json")


def test_narrated_title_lane_stamps_bend() -> None:
    # The narrated, speech-montage and voiceover-montage writers all compile their titles
    # and pinned text through this one function.
    from app.pipeline.phone_narrated_plan import _compile_title_layers

    layers = _compile_title_layers(
        [TextElement.model_validate(_bent_row())], canvas=CANVAS, timeline_duration_s=10.0
    )
    assert [layer.bend for layer in layers] == [45.0]


def test_authored_timeline_stamps_bend() -> None:
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline import test_authored_timeline as authored

    job, variant, previous = authored.fixture()
    variant["text_elements"] = [_bent_row(), _element(text="Plain", start_s=2.0, end_s=4.0)]
    recipe = compile_phone_authored_timeline(job, variant, previous)
    layers = [layer for layer in recipe.text_layers if layer.id.startswith("authored-text-")]
    assert [layer.bend for layer in layers] == [45.0, None]
    assert "textBend" in recipe.required_capabilities


# --- (c) the layer model refuses a bend its painter cannot draw --------------------------


def _layer(**changes) -> dict:
    layer = _compile(_overlay("static", bend=None)).model_dump(mode="json")
    layer.update(changes)
    return layer


def test_layer_accepts_bend_in_range_and_omits_zero() -> None:
    assert PortableTextLayer.model_validate(_layer(bend=100)).bend == 100
    assert PortableTextLayer.model_validate(_layer(bend=-100)).bend == -100
    assert "bend" not in PortableTextLayer.model_validate(_layer(bend=0)).model_dump(mode="json")
    assert "bend" not in PortableTextLayer.model_validate(_layer()).model_dump(mode="json")


@pytest.mark.parametrize("bend", [100.5, -101, float("nan")])
def test_layer_rejects_out_of_range_bend(bend) -> None:
    with pytest.raises(ValidationError):
        PortableTextLayer.model_validate(_layer(bend=bend))


@pytest.mark.parametrize(
    "effect",
    ["ink-reveal", "typewriter", "stream-in", "smooth-type", "staggered-slice", "handwriting"]
    + ["karaoke-line", "lyric-line", "dissolve-out"],
)
def test_layer_rejects_bend_on_a_reveal_effect_it_was_compiled_with(effect) -> None:
    straight = _compile(_overlay(effect, bend=None)).model_dump(mode="json")
    PortableTextLayer.model_validate(straight)  # the straight layer is valid
    with pytest.raises(ValidationError, match="bend is unsupported"):
        PortableTextLayer.model_validate({**straight, "bend": 20})


def test_layer_rejects_bend_with_each_non_bendable_payload() -> None:
    reveal = _compile(_overlay("typewriter", bend=None)).model_dump(mode="json")
    smooth = _compile(_overlay("smooth-type", bend=None)).model_dump(mode="json")
    sliced = _compile(_overlay("staggered-slice", bend=None)).model_dump(mode="json")
    pen = _compile(_overlay("handwriting", bend=None)).model_dump(mode="json")
    karaoke = _compile(_overlay("karaoke-line", bend=None)).model_dump(mode="json")
    ink = _compile(_overlay("ink-reveal", bend=None)).model_dump(mode="json")
    giant = _compile(_overlay("static", bend=None, theme_transition={"type": "giant-title-wipe"}))
    payloads = {
        "discrete_reveal": reveal["discrete_reveal"],
        "smooth_reveal": smooth["smooth_reveal"],
        "staggered": sliced["staggered"],
        "handwriting": pen["handwriting"],
        "karaoke": karaoke["karaoke"],
        "reveal_bounds": ink["reveal_bounds"],
        "giant_title": giant.model_dump(mode="json")["giant_title"],
    }
    for name, payload in payloads.items():
        # A static layer carrying the payload is invalid either way; the point is that bend
        # is named as the reason whenever it is present on a non-bendable shape.
        with pytest.raises(ValidationError):
            PortableTextLayer.model_validate(_layer(bend=30, **{name: payload}))
    # Giant title is the only payload valid on a bendable effect, so it isolates the rule.
    giant_json = giant.model_dump(mode="json")
    PortableTextLayer.model_validate(giant_json)
    with pytest.raises(ValidationError, match="bend is unsupported"):
        PortableTextLayer.model_validate({**giant_json, "bend": 30})


def test_layer_rejects_bend_with_a_typewriter_animation_phase() -> None:
    phases = {"entrance": "typewriter", "exit": "none", "loop": "none", "speed": 1}
    with pytest.raises(ValidationError, match="bend is unsupported"):
        PortableTextLayer.model_validate(_layer(bend=30, animation_phases=phases))


# --- (d) protocol gate, capability and kill switch ---------------------------------------


def test_defaults() -> None:
    assert settings.text_bend_enabled is True
    assert settings.text_bend_min_client_protocol == 8


@pytest.mark.parametrize(
    ("protocol", "expected"), [(None, False), (7, False), (8, True), (9, True)]
)
def test_client_gate_inside_a_request(protocol, expected) -> None:
    set_client_protocol(protocol)
    assert text_bend_client_supported() is expected


def test_client_gate_outside_a_request_is_open() -> None:
    assert text_bend_client_supported() is True  # workers and chat edits
    assert text_bend_client_supported(require_client=True) is False


def test_supported_needs_flag_verified_feature_and_build(monkeypatch) -> None:
    set_client_protocol(8)
    assert text_bend_supported() is False  # not verified on device yet
    _verify_bend(monkeypatch)
    assert text_bend_supported() is True
    monkeypatch.setattr(settings, "text_bend_enabled", False)
    assert text_bend_supported() is False


def _variant(destination: str) -> dict:
    return {"render_destination": destination}


@pytest.mark.parametrize(
    ("flag", "verified", "protocol", "destination", "expected"),
    [
        (True, True, 8, "device", True),
        (True, True, 8, "phone", True),
        (True, True, 7, "device", False),
        (True, True, None, "device", False),
        (True, True, 8, "cloud", False),
        (True, True, 8, None, False),
        (True, False, 8, "device", False),
        (False, True, 8, "device", False),
    ],
)
def test_editor_capability_text_bend(
    monkeypatch, flag, verified, protocol, destination, expected
) -> None:
    monkeypatch.setattr(settings, "text_bend_enabled", flag)
    if verified:
        _verify_bend(monkeypatch)
    set_client_protocol(protocol)
    assert gj._text_bend_capability(_variant(destination)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "protocol", "expected"),
    [(True, None, False), (True, 7, False), (True, 8, True), (False, 8, False)],
)
async def test_creation_capabilities_text_bend(monkeypatch, flag, protocol, expected) -> None:
    from types import SimpleNamespace

    from app.routes.creation_threads import CreationCapabilitiesOut, capabilities

    monkeypatch.setattr(settings, "text_bend_enabled", flag)
    _verify_bend(monkeypatch)
    caps = await capabilities(SimpleNamespace(id=uuid.uuid4()), False, protocol)
    assert caps["text_bend"] is expected
    assert CreationCapabilitiesOut.model_fields["text_bend"].default is False


def test_recipe_requires_text_bend_iff_a_layer_is_bent(monkeypatch) -> None:
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline import test_authored_timeline as authored

    job, variant, previous = authored.fixture()
    variant["text_elements"] = [_element(size_px=64)]
    straight = compile_phone_authored_timeline(job, variant, previous)
    assert "textBend" not in straight.required_capabilities

    variant["text_elements"] = [_element(size_px=64, bend=30)]
    bent = compile_phone_authored_timeline(job, variant, previous)
    assert "textBend" in bent.required_capabilities

    # validate_phone_pilot_recipe refuses a bent recipe until the device gate is open.
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in bent.required_capabilities if f != "textBend"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(bent, allow_editor_media=True)
    assert "textBend" in str(refused.value.capability)


# --- the Save path (real editor commit, the guided archetype's own fixtures) -------------


def _guided_env(monkeypatch):
    from tests.routes import test_editor_commit_drawings as drawings

    env = drawings._guided(monkeypatch)
    _verify_bend(monkeypatch)
    element = copy.deepcopy(env.variant["text_elements"][0])
    return env, element


def test_a_bend_save_is_accepted_from_the_minimum_build_and_pins_the_capability(
    monkeypatch,
) -> None:
    env, element = _guided_env(monkeypatch)
    set_client_protocol(settings.text_bend_min_client_protocol)

    env.save(text_elements=[{**element, "bend": 40}])

    assert "textBend" in env.recipe.required_capabilities
    assert [layer.bend for layer in env.recipe.text_layers if layer.bend] == [40.0]
    assert any(row.get("bend") == 40.0 for row in env.variant["text_elements"])

    # Straightening it removes the capability an older build could not decode.
    env.save(text_elements=[{**element, "bend": None}])
    assert "textBend" not in env.recipe.required_capabilities
    assert all("bend" not in layer.model_dump(mode="json") for layer in env.recipe.text_layers)


@pytest.mark.parametrize("protocol", [4, 7, None])
def test_an_old_build_saving_bent_text_is_told_to_update(monkeypatch, protocol) -> None:
    env, element = _guided_env(monkeypatch)
    pinned = copy.deepcopy(env.job.assembly_plan)

    set_client_protocol(protocol)
    with pytest.raises(HTTPException) as refused:
        env.save(text_elements=[{**element, "bend": 40}])

    assert refused.value.status_code == 426
    assert refused.value.detail == {"code": "client_update_required", "feature": "text_bend"}
    assert env.job.assembly_plan == pinned  # nothing was committed


def test_an_old_build_may_still_save_text_that_was_never_bent(monkeypatch) -> None:
    env, element = _guided_env(monkeypatch)
    set_client_protocol(7)
    env.save(text_elements=[{**element, "text": "Still straight"}])
    assert env.recipe.text_layers
    assert "textBend" not in env.recipe.required_capabilities


def test_an_old_build_is_refused_a_bent_stored_recipe_and_a_worker_is_not(monkeypatch) -> None:
    # A stored bent variant: a newer build (or the worker) made it.
    env, element = _guided_env(monkeypatch)
    env.save(text_elements=[{**element, "bend": 40}])  # outside any app request
    pinned = copy.deepcopy(env.job.assembly_plan)

    set_client_protocol(7)
    with pytest.raises(HTTPException) as refused:
        env.save(text_elements=[{**element, "bend": 40}])
    assert refused.value.detail["feature"] == "text_bend"
    assert env.job.assembly_plan == pinned


@pytest.mark.asyncio
async def test_device_render_poll_tells_an_old_build_to_update(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.routes import device_render

    env, element = _guided_env(monkeypatch)
    env.save(text_elements=[{**element, "bend": 40}])
    request = SimpleNamespace(recipe=env.recipe, identity=SimpleNamespace())
    monkeypatch.setattr(device_render, "_owned_job", AsyncMock(return_value=env.job))
    monkeypatch.setattr(
        device_render, "device_status", lambda *_a: SimpleNamespace(request=request)
    )
    monkeypatch.setattr(
        device_render, "_record", lambda *_a: ({}, SimpleNamespace(request=request, phase="x"))
    )
    set_client_protocol(7)
    with pytest.raises(HTTPException) as refused:
        await device_render.get_device_render(
            env.job.id, SimpleNamespace(id=uuid.uuid4()), variant_id=env.variant_id, db=AsyncMock()
        )
    assert refused.value.status_code == 426
    assert refused.value.detail == {"code": "client_update_required", "feature": "text_bend"}


# --- (e) slide PUT, editor ops ----------------------------------------------------------


@pytest.mark.asyncio
async def test_slide_put_round_trips_bend(monkeypatch) -> None:
    from tests.routes import test_slide_post_drawing_routes as slides

    asset_id = uuid.uuid4()
    item = slides._item(asset_id, None)
    text = {"id": "t1", "text": "Lisbon", "bend": 70, "rotation_deg": 5}
    await slides._put(monkeypatch, item, slides._body(asset_id, {"texts": [text]}))
    saved = item.slide_post["slides"][0]["edits"]["texts"][0]
    assert saved["bend"] == 70.0

    # A legacy single-text edit (older app / web) keeps the stored bend.
    item.slide_post["version"] = 3
    legacy = slides.plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"text": {"content": "Porto", "position": "bottom"}},
                }
            ],
            "expected_version": 3,
        }
    )
    await slides._put(monkeypatch, item, legacy)
    kept = item.slide_post["slides"][0]["edits"]["texts"][0]
    assert kept["text"] == "Porto" and kept["bend"] == 70.0

    # And an unbent text never grows the key.
    item.slide_post["version"] = 4
    await slides._put(
        monkeypatch,
        item,
        slides.plan_items.SlidePostDraftBody.model_validate(
            {
                "platform_profile": "tiktok_photo",
                "slides": [
                    {
                        "id": "slide",
                        "asset_id": str(asset_id),
                        "kind": "image",
                        "edits": {"texts": [{"id": "t1", "text": "Lisbon"}]},
                    }
                ],
                "expected_version": 4,
            }
        ),
    )
    assert "bend" not in item.slide_post["slides"][0]["edits"]["texts"][0]


def test_slide_chat_edit_reports_bend_as_unsupported() -> None:
    from app.services.slide_post_chat_edit import _UNSUPPORTED_PATCH_FIELDS

    assert "bend" in _UNSUPPORTED_PATCH_FIELDS


def test_editor_ops_keep_bend_through_patch_snapshot_and_receipt(monkeypatch) -> None:
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
    from app.services.kria_editor_ops_diff import describe_diff
    from tests.services import test_kria_editor_ops as ops
    from tests.services import test_kria_editor_ops_diff as diff_ops

    variant = ops._variant()
    variant["text_elements"][0]["bend"] = -30.0
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )
    assert build_editor_snapshot(ops._job(variant), variant)["text_bars"][0]["bend"] == -30.0

    compiled = compile_editor_ops(
        ops._job(variant),
        variant,
        [{"op": "patch_text_style", "bar_index": 0, "patch": {"bend": 55}}],
    )
    saved = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    assert saved[0]["bend"] == 55

    # The receipt names the change instead of dropping it as an unknown field.
    receipt = diff_ops._compile(diff_ops.ROWS, [diff_ops._patch(diff_ops.IDS[:1], {"bend": 40})])
    assert {f"{e.lane}.{e.field}" for e in receipt.diff.entries} == {"text.bend"}
    assert describe_diff(receipt.diff.entries, live_text_count=3) == [
        "Changed the Bend of 'Lisbon'"
    ]
    # An unchanged bend is not a change.
    same = diff_ops._compile(
        [diff_ops._row("a", "Lisbon", bend=40.0)],
        [diff_ops._patch(["a"], {"bend": 40})],
    )
    assert not same.diff.entries


def test_copy_style_carries_bend_to_the_new_bar() -> None:
    from app.services.kria_editor_ops_text import _COPY_STYLE_KEYS

    # `add_text style_from=<bar>` copies exactly these keys from the source row, so a
    # missing key silently straightens every duplicated bend.
    assert "bend" in _COPY_STYLE_KEYS
