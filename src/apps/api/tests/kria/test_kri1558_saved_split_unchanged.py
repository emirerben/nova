"""KRI-1558: a split edit saved before speaker sections existed reopens and resumes unchanged.

The fixture is a device render request that origin/main pinned for an authored edit with two
back-to-back pieces of one take drawn differently (full screen, then a custom corner) over a
speaker background. With `SPEAKER_SECTIONS_ENABLED` off, this branch must read that saved request
back byte-for-byte: same recipe, same capabilities, same fingerprint, and a phone on the build
before speaker sections (protocol 56) must not be told to update.

Ways this could fail (each is one assertion below):
* reopening the stored request raises "recipe digest mismatch" because re-validation added a
  capability the saved recipe never had;
* the request reads back but its recipe differs from what was saved (a new key, a new
  capability), so the phone would render something else than what the creator approved;
* the publish check (`_recipe_digest` against the pinned record) refuses the finished export;
* the protocol-56 build that saved it gets a 426 "update the app" for a feature that is off;
* saving the same edit again (the editor compiles the recipe without capabilities and the model
  derives them) produces a different fingerprint than main did.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.device_render import DeviceRenderStatus, recipe_digest
from app.kria.recipes_v2 import EditRecipeV2
from app.routes.device_render import _client_update_detail
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import _recipe_digest

FIXTURE = Path(__file__).parents[1] / "fixtures/kri1558_saved_split_request_main.json"


@pytest.fixture
def saved(monkeypatch):
    monkeypatch.setattr(settings, "speaker_sections_enabled", False)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "speakerSections"],
    )
    stored = json.loads(FIXTURE.read_text())
    yield stored
    clear_request_context()


def test_saved_split_edit_reopens_with_the_same_recipe_and_fingerprint(saved) -> None:
    pinned = saved["status"]
    clips = pinned["request"]["recipe"]["tracks"][0]["clips"]
    assert [clip["speaker_frame_override"]["mode"] for clip in clips] == ["full", "custom"]

    status = DeviceRenderStatus.model_validate(copy.deepcopy(pinned))

    # `required_capabilities` is a set whose list order follows the process's hash seed, so both
    # sides compare it sorted, as every fingerprint does (`canonical_recipe_document`).
    reread, expected = status.model_dump(mode="json"), copy.deepcopy(pinned)
    for document in (reread, expected):
        document["request"]["recipe"]["required_capabilities"].sort()
    assert reread == expected
    assert recipe_digest(status.request.recipe) == pinned["request"]["identity"]["recipe_digest"]
    assert _recipe_digest(status.request.recipe) == saved["recipe_digest"]
    assert "speakerSections" not in status.request.recipe.required_capabilities


def test_saved_split_edit_resumes_on_the_build_before_speaker_sections(saved) -> None:
    status = DeviceRenderStatus.model_validate(copy.deepcopy(saved["status"]))
    set_client_protocol(56)
    job = SimpleNamespace(assembly_plan={})
    assert _client_update_detail(job, {}, status) is None


def test_saving_the_same_split_edit_again_keeps_mains_fingerprint(saved) -> None:
    document = copy.deepcopy(saved["status"]["request"]["recipe"])
    document["required_capabilities"] = []
    recompiled = EditRecipeV2.model_validate(document)
    assert recipe_digest(recompiled) == saved["recipe_digest"]
