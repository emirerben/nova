#!/usr/bin/env python3
"""Compile the redacted approved-hook journey for a real native export; no paid calls."""

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))

import tests.conftest  # noqa: F401, E402 - isolated test settings, no credentials
from app.kria.device_render import DeviceRenderStatus, make_device_request  # noqa: E402
from app.kria.media_sources import OriginalMediaDescriptor  # noqa: E402
from app.pipeline.guided_story import (  # noqa: E402
    GuidedStoryExecutionPlan,
    compile_proposal_execution_plan,
)  # noqa: E402
from app.pipeline.phone_guided_plan import compile_phone_guided_plan  # noqa: E402
from app.services.creation_text_composition import compose_creation_text  # noqa: E402
from app.services.creator_render_contract import (  # noqa: E402
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)  # noqa: E402
from app.services.phone_sources import PhoneSourceBinding  # noqa: E402
from tests.evals.request_following.test_approved_hook_journey import (  # noqa: E402
    CASE,
    CTX,
    approved_plan,
)  # noqa: E402


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    plan, brief, clip = approved_plan()
    for message in [
        CASE["context"] + "\nUse this wording",
        "Try generating this edit again",
    ]:
        plan.snapshot = compose_creation_text(
            plan.snapshot,
            creator_request=message,
            text_requirements=brief.live(),
            ctx=CTX,
        )
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_proposal_execution_plan(plan.snapshot)
    )
    media = out / "hook-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x404040:s=1080x1920:r=30:d=4.1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4.1",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ],
        check=True,
    )
    binding = PhoneSourceBinding(
        media_id=clip.media_id,
        proxy_path=clip.proxy_path,
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            byte_count=media.stat().st_size,
            duration_s=4.1,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )
    recipe = compile_phone_guided_plan(execution, (binding,))
    contract = CreatorRenderContract(
        generation_id="kri651",
        exact_texts=(TextRequirement(role="opening", text=CASE["candidate"]),),
    ).rebind()
    verify_phone_recipe(contract, recipe, source_audio={clip.media_id: True})
    status = DeviceRenderStatus(
        phase="awaiting_device",
        request=make_device_request(
            job_id=uuid.uuid4(), variant_id="approved_hook", revision=1, recipe=recipe
        ),
    )
    (out / "status-approved-hook.json").write_text(status.model_dump_json(indent=2))
    (out / "server-plan.json").write_text(execution.model_dump_json(indent=2))
    meta = {
        "verified_features": sorted(recipe.required_capabilities),
        "cases": {
            "approved_hook": {
                "status_file": "status-approved-hook.json",
                "duration_s": recipe.duration,
                "required_capabilities": sorted(recipe.required_capabilities),
                "drop_capability": "authoredText",
                "clips": [{"media_id": clip.media_id, "file": media.name}],
                "expected_copy": CASE["candidate"],
                "expect_color_emoji": True,
                "samples": [
                    {
                        "name": "approved-hook",
                        "t": 0.5,
                        "x": 80,
                        "y": 1800,
                        "rgb": [64, 64, 64],
                    }
                ],
                "caption_samples": [
                    {
                        "name": "title-present",
                        "t": 0.5,
                        "region": [0, 0, 1080, 650],
                        "expect_text": True,
                    },
                    {
                        "name": "no-context-caption",
                        "t": 0.5,
                        "region": [0, 1250, 1080, 650],
                        "expect_text": False,
                    },
                ],
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                "result": "compiled",
                "copy": CASE["candidate"],
                "text_model_calls": 0,
                "output": str(out),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
