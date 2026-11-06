#!/usr/bin/env python3
"""Item 22 evidence: compile Qendresa's Q3 request for a real native export; no paid calls.

The AI text composer is faked with one invented hook line. With the asked-only switch off
that line reaches the recipe; with it on (after the fix) only her typed line should remain.

    python scripts/ios/qendresa-q3-asked-only-e2e.py <out_dir> --switch off|on --source <mp4>
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))

from app.agents._runtime import ModelInvocation, RunContext  # noqa: E402
from app.config import settings  # noqa: E402
from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: E402
from app.kria.device_render import DeviceRenderStatus, make_device_request  # noqa: E402
from app.kria.media_sources import OriginalMediaDescriptor  # noqa: E402
from app.pipeline.guided_story import (  # noqa: E402
    GuidedStoryExecutionPlan,
    compile_proposal_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan  # noqa: E402
from app.pipeline.unified_montage import (  # noqa: E402
    UnifiedClip,
    brief_view,
    plan_unified_montage,
    skia_font_covers,
)
from app.services import creation_text_composition as composition  # noqa: E402
from app.services.phone_sources import PhoneSourceBinding  # noqa: E402

T3 = "The prompt I run at the end of every Claude session"
PROMPT = (
    'Just put this text in the middle for the whole video: "The prompt I run at the end of '
    "every Claude session\". Small white text like TikTok's, no captions and nothing else. "
    "Keep the whole clip, don't cut it. Mute my sound, I'll add a song on TikTok."
)
RESPONSE = {
    "intent": "edit",
    "confidence": 1,
    "reply": "Added a hook",
    "ops": [
        {"op": "add_text", "text": "Wait till the end 👀", "start_s": 0, "end_s": 2.5}
    ],
}
DURATION_S = 11.0


class FakeClient:
    """Stands in for the model client: counts calls, returns the invented hook."""

    def __init__(self):
        self.calls = 0

    def invoke(self, **kw):
        self.calls += 1
        return ModelInvocation(raw_text=json.dumps(RESPONSE))


def build_plan():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="global",
                literal=T3,
                description="small white TikTok-style text in the middle for the whole video",
                facts={"position": "middle"},
            ),
            BriefRequirement(
                id="r2",
                kind="style",
                scope="global",
                description="no captions",
                facts={"captions": "off"},
            ),
            BriefRequirement(
                id="r3",
                kind="style",
                scope="global",
                description="no other text, nothing else",
            ),
            BriefRequirement(
                id="r4",
                kind="timing",
                scope="global",
                description="keep the whole clip, don't cut it",
                facts={"keep_whole_take": True},
            ),
            BriefRequirement(
                id="r5",
                kind="audio",
                scope="global",
                description="mute the clip's own sound",
                facts={"clip_sound": "off"},
            ),
        ],
    )
    clip = UnifiedClip(
        media_id="clip-1",
        proxy_path="users/u/analysis-proxy-clip-1.mp4",
        generation="1",
        duration_s=DURATION_S,
        width=1080,
        height=1920,
        analysis={"best_moments": []},
    )
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "direction": "fast_montage",
        "pinned_texts": [{"text": T3, "corner": "top_center"}],
    }
    plan = plan_unified_montage(
        [clip], brief_view(brief), strategy=strategy, font_covers=skia_font_covers
    )
    return plan, brief, clip


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(out.stdout)
    return {
        "width": data["streams"][0]["width"],
        "height": data["streams"][0]["height"],
        "duration": float(data["format"]["duration"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir")
    parser.add_argument("--switch", choices=["off", "on"], required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    source = Path(args.source)
    media = out / "q3-source.mp4"
    shutil.copyfile(source, media)
    info = probe(media)

    settings.text_composer_asked_only_enabled = args.switch == "on"

    plan, brief, clip = build_plan()
    fake = FakeClient()
    composition.default_client = lambda: fake
    from app.services.creation_text_composition import (  # noqa: PLC0415
        compose_creation_text,
        snapshot_copy_constraints,
    )

    plan.snapshot = compose_creation_text(
        plan.snapshot,
        creator_request=PROMPT,
        text_requirements=brief.live(),
        copy_constraints=snapshot_copy_constraints(plan.snapshot),
        ctx=RunContext(extra={"skip_agent_run_persist": True}),
    )
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_proposal_execution_plan(plan.snapshot)
    )
    binding = PhoneSourceBinding(
        media_id=clip.media_id,
        proxy_path=clip.proxy_path,
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            byte_count=media.stat().st_size,
            duration_s=DURATION_S,
            width=info["width"],
            height=info["height"],
            has_audio=True,
        ),
    )
    recipe = compile_phone_guided_plan(execution, (binding,))
    status = DeviceRenderStatus(
        phase="awaiting_device",
        request=make_device_request(
            job_id=uuid.uuid4(), variant_id="approved_hook", revision=1, recipe=recipe
        ),
    )
    (out / "status-approved-hook.json").write_text(status.model_dump_json(indent=2))
    (out / "server-plan.json").write_text(execution.model_dump_json(indent=2))
    capabilities = sorted(
        c.value if hasattr(c, "value") else c for c in recipe.required_capabilities
    )
    meta = {
        "verified_features": capabilities,
        "cases": {
            "approved_hook": {
                "status_file": "status-approved-hook.json",
                "duration_s": recipe.duration,
                "required_capabilities": capabilities,
                "drop_capability": capabilities[0],
                "clips": [{"media_id": clip.media_id, "file": media.name}],
                # The footage changes every frame, so no fixed-pixel probes; the score is
                # taken from the exported MP4 by the Q3 checker instead.
                "samples": [],
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    text_elements = execution.model_dump().get("text_elements") or []
    print(
        json.dumps(
            {
                "switch": args.switch,
                "composer_calls": fake.calls,
                "text_bars": [
                    {
                        "text": t.get("text"),
                        "start": t.get("start_s", t.get("start")),
                        "end": t.get("end_s", t.get("end")),
                        "y": t.get("y_frac")
                        if t.get("y_frac") is not None
                        else t.get("position"),
                    }
                    for t in text_elements
                ],
                "recipe_text_layers": [
                    {
                        "id": layer.id,
                        "text": "".join(r.text for r in layer.runs),
                        "start": layer.start,
                        "end": layer.end,
                    }
                    for layer in recipe.text_layers
                ],
                "duration_s": recipe.duration,
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
