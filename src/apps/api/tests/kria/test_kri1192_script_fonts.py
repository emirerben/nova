"""KRI-1192 F16: an iPhone build that does not bundle the script faces never gets them.

Noto Sans KR / Noto Sans Arabic ship in the app from protocol 35. A phone recipe whose text
is drawn in one of them names the `scriptFonts` capability; an older build (or a server whose
device check has not verified the token) must be refused with "update the app", never handed
a recipe that would fail on the phone for a font file it does not have.

Failure modes:
 S1  a recipe draws Hangul/Arabic in a script face without naming `scriptFonts`
 S2  a Latin/Greek/Cyrillic recipe suddenly requires `scriptFonts` (old builds refused for
     nothing)
 S3  validate admits a script-face recipe before `scriptFonts` is verified on device
 S4  a build below protocol 35 polling or saving such a recipe gets it instead of a 426
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.font_coverage import script_fallback_files
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    recipe_uses_script_fonts,
    script_fonts_client_supported,
    script_fonts_planned_for,
    validate_phone_pilot_recipe,
)
from app.services.render_library import bundled_font_asset


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _verified(monkeypatch, *extra: str) -> None:
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *extra],
    )


def _authored_recipe(font_file: str | None) -> EditRecipeV2:
    """A real phone recipe (authored timeline fixture) with its text font swapped to
    ``font_file`` and re-validated, so the capability set is derived again."""
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline import test_authored_timeline as authored

    job, variant, previous = authored.fixture()
    variant["text_elements"] = [
        {"text": "Hello", "start_s": 0.0, "end_s": 2.0, "role": "generative_intro", "size_px": 64}
    ]
    recipe = compile_phone_authored_timeline(job, variant, previous)
    if font_file is None:
        return recipe
    data = recipe.model_dump(mode="json")
    font_ids = {run["font_asset_id"] for layer in data["text_layers"] for run in layer["runs"]}
    assert font_ids
    swapped = {
        font_id: bundled_font_asset(font_file, asset_id=font_id).model_dump(mode="json")
        for font_id in font_ids
    }
    data["asset_manifest"]["assets"] = [
        swapped.get(asset["id"], asset) for asset in data["asset_manifest"]["assets"]
    ]
    for asset in data["assets"]:
        if asset["id"] in swapped:
            fingerprint = swapped[asset["id"]]["fingerprint"]
            asset["fingerprint"] = {
                "algorithm": "sha256",
                "hex": fingerprint["sha256"],
                "byte_count": fingerprint["byte_count"],
            }
    data["required_capabilities"] = sorted(set(data["required_capabilities"]) - {"scriptFonts"})
    return EditRecipeV2.model_validate(data)


def test_the_script_faces_are_the_noto_files():
    assert script_fallback_files() == frozenset(
        {
            "NotoSansKR-Regular.ttf",
            "NotoSansKR-Bold.ttf",
            "NotoSansArabic-Regular.ttf",
            "NotoSansArabic-Bold.ttf",
        }
    )


@pytest.mark.parametrize("font_file", sorted(script_fallback_files()))
def test_a_script_face_names_the_capability(font_file):  # S1
    recipe = _authored_recipe(font_file)
    assert "scriptFonts" in recipe.required_capabilities
    assert recipe_uses_script_fonts(recipe)


@pytest.mark.parametrize("font_file", [None, "TikTokSans-Bold.ttf", "Inter-Bold.ttf"])
def test_other_faces_do_not(font_file):  # S2 (TikTok Sans and Inter draw Greek and Cyrillic)
    recipe = _authored_recipe(font_file)
    assert "scriptFonts" not in recipe.required_capabilities
    assert not recipe_uses_script_fonts(recipe)


def test_validate_refuses_until_the_device_check_verified_it(monkeypatch):  # S3
    recipe = _authored_recipe("NotoSansKR-Bold.ttf")
    _verified(monkeypatch, *[c for c in recipe.required_capabilities if c != "scriptFonts"])
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe, allow_editor_media=True)
    assert refused.value.capability == "scriptFonts"
    _verified(monkeypatch, "scriptFonts")
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)


def test_defaults():
    assert settings.script_fonts_min_client_protocol == 35


@pytest.mark.parametrize(("protocol", "expected"), [(None, False), (34, False), (35, True)])
def test_client_gate_inside_a_request(protocol, expected):  # S4
    set_client_protocol(protocol)
    assert script_fonts_client_supported() is expected


def test_client_gate_outside_a_request_is_open():
    assert script_fonts_client_supported() is True  # workers
    assert script_fonts_client_supported(require_client=True) is False


def test_planning_needs_the_verified_token_and_the_build(monkeypatch):
    assert script_fonts_planned_for(35) is False  # not verified on device yet
    _verified(monkeypatch, "scriptFonts")
    assert script_fonts_planned_for(35) is True
    assert script_fonts_planned_for(34) is False
    assert script_fonts_planned_for(None) is False


def test_the_update_detail_names_script_fonts():  # S4
    from app.services.phone_rollout import script_fonts_update_detail

    recipe = _authored_recipe("NotoSansArabic-Bold.ttf")
    set_client_protocol(34)
    assert script_fonts_update_detail(recipe) == {
        "code": "client_update_required",
        "feature": "script_fonts",
        "minimum_client_protocol": 35,
    }
    set_client_protocol(35)
    assert script_fonts_update_detail(recipe) is None
    set_client_protocol(34)
    assert script_fonts_update_detail(_authored_recipe("TikTokSans-Bold.ttf")) is None


@pytest.mark.asyncio
async def test_device_render_poll_tells_an_old_build_to_update(monkeypatch):  # S4, wired
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.routes import device_render
    from tests.kria.test_text_bend import _guided_env

    env, _element = _guided_env(monkeypatch)
    request = SimpleNamespace(
        recipe=_authored_recipe("NotoSansKR-Bold.ttf"), identity=SimpleNamespace(), recovery=None
    )
    monkeypatch.setattr(device_render, "_owned_job", AsyncMock(return_value=env.job))
    monkeypatch.setattr(
        device_render, "device_status", lambda *_a: SimpleNamespace(request=request)
    )
    monkeypatch.setattr(
        device_render, "_record", lambda *_a: ({}, SimpleNamespace(request=request, phase="x"))
    )
    set_client_protocol(34)
    with pytest.raises(HTTPException) as refused:
        await device_render.get_device_render(
            env.job.id, SimpleNamespace(id=uuid.uuid4()), variant_id=env.variant_id, db=AsyncMock()
        )
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "script_fonts"
