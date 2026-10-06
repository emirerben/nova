from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import (
    CONTRACT_REVISIONS_FIELD,
    REQUIREMENT_VERSION_FIELD,
    _contract_for_variant,
    device_record,
    pin_device_request,
)


def _legacy_recipe() -> EditRecipeV1:
    return EditRecipeV1(
        assets=[{"id": "clip", "relative_path": "clip"}],
        tracks=[
            {
                "id": "video",
                "kind": "video",
                "clips": [
                    {
                        "id": "cut",
                        "source_asset_id": "clip",
                        "source_start": 0,
                        "source_duration": 2,
                        "timeline_start": 0,
                        "rate": 1,
                    }
                ],
            }
        ],
    )


def test_legacy_recipe_stays_byte_compatible_without_contract() -> None:
    job = SimpleNamespace(id=uuid4(), assembly_plan={})
    request = make_device_request(
        job_id=job.id, variant_id="v", revision=1, recipe=_legacy_recipe()
    )

    assert pin_device_request(job, request, base_generation="approved") is True
    record = device_record(job, "v")
    assert "contract_digest" not in record
    assert record["status"]["request"] == request.model_dump(mode="json")


def test_contract_requires_v2_before_a_device_recipe_is_pinned() -> None:
    job = SimpleNamespace(id=uuid4(), assembly_plan={})
    contract = build_render_contract({"opening_title": "Approved"}, generation_id="approved")
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    request = make_device_request(
        job_id=job.id, variant_id="v", revision=1, recipe=_legacy_recipe()
    )

    with pytest.raises(CreatorRenderContractError, match="current phone renderer"):
        pin_device_request(job, request, base_generation="approved")


def test_marked_job_cannot_fall_back_to_legacy_when_root_is_removed() -> None:
    job = SimpleNamespace(
        id=uuid4(),
        all_candidates={REQUIREMENT_VERSION_FIELD: 1},
        assembly_plan={},
    )
    request = make_device_request(
        job_id=job.id, variant_id="v", revision=1, recipe=_legacy_recipe()
    )

    with pytest.raises(CreatorRenderContractError, match="unavailable"):
        pin_device_request(job, request, base_generation="approved")


def test_editor_revision_does_not_replace_sibling_root_authority() -> None:
    root = build_render_contract({"opening_title": "Original"}, generation_id="first")
    revised = build_render_contract({"opening_title": "Edited"}, generation_id="second")
    assert root is not None and revised is not None
    assembly = {
        CONTRACT_FIELD: root.model_dump(mode="json"),
        CONTRACT_REVISIONS_FIELD: {"edited": revised.model_dump(mode="json")},
    }

    assert _contract_for_variant(assembly, "edited") == revised
    assert _contract_for_variant(assembly, "sibling") == root


def test_malformed_editor_revision_never_falls_back_to_root_authority() -> None:
    root = build_render_contract({"opening_title": "Original"}, generation_id="first")
    assert root is not None
    assembly = {
        CONTRACT_FIELD: root.model_dump(mode="json"),
        CONTRACT_REVISIONS_FIELD: {"edited": None},
    }

    with pytest.raises(CreatorRenderContractError, match="read"):
        _contract_for_variant(assembly, "edited")


def test_required_camera_audio_rejects_a_source_known_to_have_no_audio() -> None:
    # A render manifest says what is referenced, but not whether a phone
    # original actually contains an audio stream.  That fact comes only from
    # the server-owned PhoneSourceBinding receipt passed by the pin path.
    from app.kria.render_assets import OriginalRenderAsset, RenderFingerprint

    asset = OriginalRenderAsset(
        id="talk",
        media_id="talk",
        fingerprint=RenderFingerprint(sha256="a" * 64, byte_count=1),
    )
    recipe = SimpleNamespace(
        asset_manifest=SimpleNamespace(assets=(asset,)),
        duration=1.0,
        tracks=[
            SimpleNamespace(
                kind="video",
                clips=[
                    SimpleNamespace(
                        id="clip",
                        source_asset_id="talk",
                        volume=1,
                        timeline_start=0.0,
                        source_duration=1.0,
                        rate=1.0,
                    )
                ],
            )
        ],
        audio=SimpleNamespace(original_volume=1.0, mute_windows=[]),
    )
    contract = CreatorRenderContract(generation_id="approved").rebind(
        original_audio="require", audio_source_ids=("talk",)
    )
    with pytest.raises(CreatorRenderContractError, match="camera audio"):
        verify_phone_recipe(contract, recipe, source_audio={"talk": False})


def test_recipe_digest_is_stable_across_worker_hash_seeds() -> None:
    import os
    import subprocess
    import sys

    code = (
        "from tests.services.test_creator_render_contract import _speech_recipe; "
        "from app.services.device_render import _recipe_digest; "
        "print(_recipe_digest(_speech_recipe()))"
    )
    digests = {
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        ).strip()
        for seed in ("0", "1", "2")
    }
    assert len(digests) == 1, "The same approved recipe must survive a different worker process"
