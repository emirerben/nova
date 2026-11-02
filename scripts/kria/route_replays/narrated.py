"""Narrated render replay (KRI-1097 wave 2, lane L3).

Calls the route's render entry, `compile_phone_narrated_plan`, with the fixture's own plan and
asks, the way `_run_phone_narrated_job` hands them over (the plan as ``strategy``, the brief's
asks as ``requirements``, authored text rows as a list). Everything else comes from the
fixture's clips: one step per clip at its full length, so the voice clock is the clips' total.
That timeline is synthetic: this replay says nothing about the recorded voice's length.
"""

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_narrated_plan import NarratedPhoneStep, compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.services.phone_sources import PhoneSourceBinding


def _binding(clip):
    return PhoneSourceBinding(
        media_id=clip["id"],
        proxy_path=f"replay/{clip['id']}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=clip["duration_s"],
            width=1080,
            height=1920,
            has_audio=bool(clip.get("has_audio", True)),
        ),
    )


def recipe(fx):
    clips = fx["clips"]
    steps = []
    cursor = 0.0
    for index, clip in enumerate(clips):
        end = cursor + float(clip["duration_s"])
        steps.append(
            NarratedPhoneStep(
                step_id=f"step-{index}", media_id=clip["id"], start_s=cursor, end_s=end
            )
        )
        cursor = end
    narration = PhoneNarrationBed(
        plan_item_id="replay-item",
        generation="1",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=1000),
        duration_s=cursor,
    )
    return compile_phone_narrated_plan(
        steps,
        tuple(_binding(clip) for clip in clips),
        narration,
        voiceover_duration_s=cursor,
        resolved_text_elements=[],
        strategy=fx["strategy"],
        requirements=fx["requirements"],
    )
