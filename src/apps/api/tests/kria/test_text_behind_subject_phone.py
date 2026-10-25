"""Text behind people on the phone -- the server half.

The iPhone computes the person mask and occludes the layer (`KriaMediaEngine.NativeSubjectMatte`);
the server only STAMPS `behind_subject` on a phone recipe text layer when the device gate is
open, STRIPS it (plain text + fallback trace) otherwise, and GATES old app builds that would
fail to decode the key. Failure modes F9/F10/F14/F15 of
`docs/reviews/text-behind-subject-phone-failure-modes.md`:

 F10a flag off: a flagged text element compiles byte-identically to one that never asked
 F10b `behindSubject` not in PHONE_RENDER_VERIFIED_FEATURES: stripped
 F9   build below `text_behind_subject_phone_min_client_protocol`: stripped on its own save,
      426 `client_update_required` when it reads a pinned recipe that already carries the key
 F14  a giant-title layer never carries the flag (its own compositing lane)
 F15  the capability is derived from the layers and refused by the pilot validator unless verified
"""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.portable_text import PortableTextLayer
from app.pipeline.canvas import Canvas
from app.pipeline.generative_overlays import build_intro_overlay
from app.pipeline.portable_text_layout import (
    BEHIND_SUBJECT_PLAIN_TEXT_NOTE,
    compile_text_overlay,
)
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    text_behind_subject_phone_client_supported,
    text_behind_subject_phone_supported,
    validate_phone_pilot_recipe,
)

CANVAS = Canvas(1080, 1920)


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _open_gate(monkeypatch, *, flag: bool = True, verified: bool = True) -> None:
    monkeypatch.setattr(settings, "text_behind_subject_phone_enabled", flag)
    features = [f for f in settings.phone_render_verified_features if f != "behindSubject"]
    if verified:
        features.append("behindSubject")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _overlay(*, behind_subject: bool, effect: str = "static", **extra) -> dict:
    overlay = build_intro_overlay(
        "Hello behind you",
        effect=effect,
        start_s=0.0,
        end_s=3.0,
        text_size_px=64,
        behind_subject=behind_subject,
    )
    assert overlay is not None
    overlay["effect"] = effect
    overlay.update(extra)
    return overlay


def _compile(overlay: dict) -> PortableTextLayer:
    return compile_text_overlay(overlay, layer_id="behind", canvas=CANVAS)[0]


# --- defaults -------------------------------------------------------------------------------


def test_defaults_are_dark() -> None:
    assert settings.text_behind_subject_phone_enabled is False
    assert settings.text_behind_subject_phone_min_client_protocol == 21
    assert "behindSubject" not in set(settings.phone_render_verified_features)


# --- F10: flag off / token missing strip to plain text, byte-identically ---------------------


def test_flag_off_is_byte_identical_to_a_layer_that_never_asked(monkeypatch) -> None:
    _open_gate(monkeypatch, flag=False, verified=True)
    set_client_protocol(21)
    asked = _compile(_overlay(behind_subject=True))
    plain = _compile(_overlay(behind_subject=False))
    assert asked.behind_subject is None
    assert asked.model_dump(mode="json") == plain.model_dump(mode="json")
    assert "behind_subject" not in asked.model_dump(mode="json")


def test_verified_token_missing_strips(monkeypatch) -> None:
    _open_gate(monkeypatch, flag=True, verified=False)
    set_client_protocol(21)
    assert _compile(_overlay(behind_subject=True)).behind_subject is None
    assert text_behind_subject_phone_supported() is False


def test_open_gate_stamps_the_layer_and_the_wire_carries_it(monkeypatch) -> None:
    _open_gate(monkeypatch)
    set_client_protocol(21)
    layer = _compile(_overlay(behind_subject=True))
    assert layer.behind_subject is True
    assert layer.model_dump(mode="json")["behind_subject"] is True
    # The plain twin's wire shape is unchanged by the feature existing.
    assert "behind_subject" not in _compile(_overlay(behind_subject=False)).model_dump(mode="json")


def test_strip_records_the_plain_text_fallback(monkeypatch) -> None:
    from app.pipeline import portable_text_layout as layout
    from app.services import pipeline_trace

    _open_gate(monkeypatch, flag=False)
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data or {})),
    )
    layer, _font = layout.compile_text_overlay(
        _overlay(behind_subject=True), layer_id="behind", canvas=CANVAS
    )
    assert layer.behind_subject is None
    assert events == [
        (
            "overlay",
            "text_behind_subject_phone_fallback",
            {
                "layer_id": "behind",
                "reason": "phone_gate_closed",
                "note": BEHIND_SUBJECT_PLAIN_TEXT_NOTE,
            },
        )
    ]
    assert "falls back to plain text" in BEHIND_SUBJECT_PLAIN_TEXT_NOTE


# --- F9: the app-build gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "expected"), [(None, False), (9, False), (20, False), (21, True), (22, True)]
)
def test_client_gate_inside_a_request(protocol, expected) -> None:
    set_client_protocol(protocol)
    assert text_behind_subject_phone_client_supported() is expected


def test_client_gate_outside_a_request_is_open() -> None:
    assert text_behind_subject_phone_client_supported() is True  # workers
    assert text_behind_subject_phone_client_supported(require_client=True) is False


def test_protocol_below_min_strips(monkeypatch) -> None:
    _open_gate(monkeypatch)
    set_client_protocol(20)
    assert _compile(_overlay(behind_subject=True)).behind_subject is None
    set_client_protocol(21)
    assert _compile(_overlay(behind_subject=True)).behind_subject is True


def test_a_worker_with_no_app_build_stamps_when_flag_and_token_hold(monkeypatch) -> None:
    _open_gate(monkeypatch)
    clear_request_context()  # a Celery worker: no X-Kria-Client-Protocol to read
    assert _compile(_overlay(behind_subject=True)).behind_subject is True
    _open_gate(monkeypatch, verified=False)
    assert _compile(_overlay(behind_subject=True)).behind_subject is None


# --- F14: the layer model and the compile refuse a giant title ------------------------------


def test_a_giant_title_never_carries_the_flag(monkeypatch) -> None:
    _open_gate(monkeypatch)
    set_client_protocol(21)
    giant = _compile(_overlay(behind_subject=True, theme_transition={"type": "giant-title-wipe"}))
    assert giant.giant_title is not None
    assert giant.behind_subject is None
    with pytest.raises(ValidationError, match="behind_subject is unsupported"):
        PortableTextLayer.model_validate({**giant.model_dump(mode="json"), "behind_subject": True})


def test_layer_model_omits_false_and_keeps_true() -> None:
    plain = _compile(_overlay(behind_subject=False)).model_dump(mode="json")
    assert "behind_subject" not in PortableTextLayer.model_validate(
        {**plain, "behind_subject": False}
    ).model_dump(mode="json")
    assert (
        PortableTextLayer.model_validate({**plain, "behind_subject": True}).model_dump(mode="json")[
            "behind_subject"
        ]
        is True
    )


# --- F15: capability derivation + pilot validation -------------------------------------------


def _element(**changes) -> dict:
    return {"text": "Hello", "start_s": 0.0, "end_s": 2.0, "role": "generative_intro", **changes}


def test_text_element_flows_into_the_overlay_dict() -> None:
    from app.pipeline.generative_overlays import build_overlays_from_text_elements

    rows = [
        TextElement.model_validate(_element(behind_subject=True)),
        TextElement.model_validate(_element(text="Plain")),
    ]
    behind, plain = build_overlays_from_text_elements(rows, video_duration_s=5.0)
    assert behind["behind_subject"] is True
    assert "behind_subject" not in plain


def test_recipe_requires_behind_subject_iff_a_layer_is_flagged(monkeypatch) -> None:
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline import test_authored_timeline as authored

    job, variant, previous = authored.fixture()
    variant["text_elements"] = [_element(size_px=64, behind_subject=True)]

    # Gate closed (the default): the flag is stripped, no capability, nothing to verify.
    stripped = compile_phone_authored_timeline(job, variant, previous)
    assert "behindSubject" not in stripped.required_capabilities
    assert all(layer.behind_subject is None for layer in stripped.text_layers)

    _open_gate(monkeypatch)
    flagged = compile_phone_authored_timeline(job, variant, previous)
    assert "behindSubject" in flagged.required_capabilities
    assert [
        layer.behind_subject
        for layer in flagged.text_layers
        if layer.id.startswith("authored-text-")
    ] == [True]
    # With every capability the recipe needs verified (the token included), the pilot gate passes.
    monkeypatch.setattr(
        settings, "phone_render_verified_features", sorted(flagged.required_capabilities)
    )
    validate_phone_pilot_recipe(flagged, allow_editor_media=True)

    # A caller that lost the token after compile is refused rather than handed to a phone.
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in flagged.required_capabilities if f != "behindSubject"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(flagged, allow_editor_media=True)
    assert refused.value.capability == "behindSubject"


# --- the Save path (real editor commit) + the device-render read -----------------------------


def _guided_env(monkeypatch):
    from tests.routes import test_editor_commit_drawings as drawings

    env = drawings._guided(monkeypatch)
    _open_gate(monkeypatch)
    element = copy.deepcopy(env.variant["text_elements"][0])
    return env, element


def test_a_save_from_the_minimum_build_pins_the_capability_and_an_old_build_gets_plain_text(
    monkeypatch,
) -> None:
    env, element = _guided_env(monkeypatch)
    set_client_protocol(settings.text_behind_subject_phone_min_client_protocol)
    env.save(text_elements=[{**element, "behind_subject": True}])
    assert "behindSubject" in env.recipe.required_capabilities
    assert [layer.behind_subject for layer in env.recipe.text_layers if layer.behind_subject] == [
        True
    ]
    assert any(row.get("behind_subject") is True for row in env.variant["text_elements"])

    # An older build saving the same element is NOT told to update: it keeps the text, plain.
    set_client_protocol(20)
    env.save(text_elements=[{**element, "behind_subject": True}])
    assert "behindSubject" not in env.recipe.required_capabilities
    assert all(layer.behind_subject is None for layer in env.recipe.text_layers)


@pytest.mark.asyncio
async def test_device_render_poll_tells_an_old_build_to_update(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.routes import device_render

    env, element = _guided_env(monkeypatch)
    set_client_protocol(21)
    env.save(text_elements=[{**element, "behind_subject": True}])
    assert any(layer.behind_subject for layer in env.recipe.text_layers)
    request = SimpleNamespace(recipe=env.recipe, identity=SimpleNamespace(), recovery=None)
    monkeypatch.setattr(device_render, "_owned_job", AsyncMock(return_value=env.job))
    monkeypatch.setattr(
        device_render, "device_status", lambda *_a: SimpleNamespace(request=request)
    )
    monkeypatch.setattr(
        device_render, "_record", lambda *_a: ({}, SimpleNamespace(request=request, phase="x"))
    )
    set_client_protocol(20)
    with pytest.raises(HTTPException) as refused:
        await device_render.get_device_render(
            env.job.id, SimpleNamespace(id=uuid.uuid4()), variant_id=env.variant_id, db=AsyncMock()
        )
    assert refused.value.status_code == 426
    assert refused.value.detail["code"] == "client_update_required"
    assert refused.value.detail["feature"] == "text_behind_subject"
    assert refused.value.detail["minimum_client_protocol"] == 21


def test_the_production_profile_strips_the_flag(monkeypatch) -> None:
    """Prod has neither the flag nor the `behindSubject` token: today's compile is unchanged."""
    from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

    apply_prod_profile(monkeypatch)
    assert "behindSubject" not in PROD_VERIFIED_FEATURES
    set_client_protocol(21)
    assert _compile(_overlay(behind_subject=True)).behind_subject is None
