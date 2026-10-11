#!/usr/bin/env python3
"""Generate the offline KRI-492 approved-length device-export fixture.

This reuses KRI-524's captured creation transport and production guided-phone
compiler, but replaces its model-proposed 20-second plan with the creator's
approved 24-second binding before compilation.  No provider call is made.
"""
# ruff: noqa: E402
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))
os.environ.setdefault("STORAGE_BUCKET", "nova-test")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_kri492_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.config import settings
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    check_phone_dispatch_contract,
    verify_phone_recipe,
)
from app.services.length_policy import creator_duration_s
from app.services.length_policy_state import read_length_policy, stamp_length_policy
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding


def _kri524():
    path = ROOT / "scripts/ios/kri-524-creation-e2e.py"
    spec = importlib.util.spec_from_file_location("kri524_fixture", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _approved_24_plan(source: dict) -> GuidedStoryExecutionPlan:
    """Persist/reload a 24-second approved execution plan before compiling it."""
    plan = copy.deepcopy(source)
    plan["approved_duration_s"] = 24.0
    plan["resolved_duration_s"] = 24.0
    for index, moment in enumerate(plan["story_timeline"]):
        moment.update(
            source_start_s=0.0,
            source_end_s=12.0,
            output_start_s=12.0 * index,
            output_end_s=12.0 * (index + 1),
            duration_s=12.0,
        )
    for index, window in enumerate(plan["beat_windows"]):
        window.update(
            approved_duration_s=12.0,
            resolved_duration_s=12.0,
            start_s=12.0 * index,
            end_s=12.0 * (index + 1),
        )
    return GuidedStoryExecutionPlan.model_validate(plan)


def main() -> None:
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    helper = _kri524()

    # An absent creator duration remains unproven even though the product has a
    # 24-second default. The explicit approved request is the authority.
    if creator_duration_s(strategy={"target_duration_s": 24}) is not None:
        raise ValueError("model/default duration must not become creator authority")
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="duration-24", kind="timing", scope="global", description="24 seconds", facts={"duration_s": 24})],
    )
    binding = BriefBinding.create(
        uuid.uuid4(), brief, latest_message="Make this exactly 24 seconds.", media_snapshot={"clip_assignments": []}
    )
    if creator_duration_s(brief=binding.resolve()) != 24.0:
        raise ValueError("approved brief did not preserve the creator's 24-second request")

    capture = helper.captured_case()
    composed = helper.compose_from_capture(capture)
    before = helper.compile_proposal_execution_plan(composed)
    edited, _ = helper.retime(before, capture["followup"])
    # Save/reload boundary: compiler only receives the persisted approved plan.
    plan_path = output / "approved-24-plan.json"
    plan_path.write_text(_approved_24_plan(edited).model_dump_json(indent=2))
    plan = GuidedStoryExecutionPlan.model_validate_json(plan_path.read_text())
    for moment in plan.story_timeline:
        moment.gcs_path = f"users/test/analysis-proxy-{moment.media_id}.mp4"

    files = {"c0": output / "length-c0.mp4", "c1": output / "length-c1.mp4"}
    for media_id, color in (("c0", "red"), ("c1", "blue")):
        helper.color_clip(files[media_id], color)
    bindings = []
    for media_id, path in files.items():
        sha, size = helper.fingerprint(path)
        bindings.append(PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"users/test/analysis-proxy-{media_id}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(sha256=sha, byte_count=size, duration_s=12, width=1080, height=1920, has_audio=True),
        ))
    recipe = compile_phone_guided_plan(plan, tuple(bindings))
    if abs(recipe.duration - 24.0) > 1e-6:
        raise ValueError("production phone compiler did not retain approved 24 seconds")
    contract = CreatorRenderContract(
        generation_id="kri-492-approved-24", duration_s=24.0, original_audio="require", order_required=True, order_ids=("c0", "c1")
    ).rebind()
    assembly = stamp_length_policy(
        {CONTRACT_FIELD: contract.model_dump(mode="json")},
        {"edit_format": "montage", "audio_strategy": "licensed_music"},
        contract,
        mode="enforce",
    )
    policy = read_length_policy(assembly, contract)
    if check_phone_dispatch_contract(
        contract, snapshot_generation_id="kri-492-approved-24", has_voiceover_candidate=False
    ):
        raise ValueError("unexpected voiceover dispatch for the approved montage")
    try:
        read_length_policy(assembly, contract.rebind(generation_id="kri-492-stale-retry"))
    except CreatorRenderContractError:
        pass
    else:
        raise ValueError("stale retry inherited a prior approved length policy")
    settings.phone_render_verified_features = sorted(recipe.required_capabilities)
    validate_phone_pilot_recipe(recipe)
    verify_phone_recipe(contract, recipe, source_audio={"c0": True, "c1": True}, length_policy=policy)
    request = make_device_request(job_id=uuid.uuid4(), variant_id="kri492_length_24", revision=1, recipe=recipe)
    (output / "status-kri492-length-24.json").write_text(DeviceRenderStatus(phase="awaiting_device", request=request).model_dump_json(indent=2))
    (output / "e2e.json").write_text(json.dumps({"verified_features": sorted(recipe.required_capabilities), "cases": {"kri492_length_24": {
        "status_file": "status-kri492-length-24.json", "duration_s": recipe.duration,
        "required_capabilities": sorted(recipe.required_capabilities), "drop_capability": "positionedText",
        "clips": [{"media_id": "c0", "file": "length-c0.mp4"}, {"media_id": "c1", "file": "length-c1.mp4"}],
        "samples": [{"name": "first", "t": 1, "x": 80, "y": 1800, "rgb": [255, 0, 0]}, {"name": "second", "t": 13, "x": 80, "y": 1800, "rgb": [0, 0, 255]}],
        "audio_samples": [{"name": "first_audio", "t": 1, "speaker_hz": 440, "muted_hz": 660}, {"name": "second_audio", "t": 13, "speaker_hz": 440, "muted_hz": 660}],
        "model_transport_provenance": {"creation": "authored_model_transport_fixture", "approval": "creator_brief_binding_24_seconds"}
    }}}, indent=2))
    print(f"KRI-492 approved-length fixture written to {output}")


if __name__ == "__main__":
    main()
