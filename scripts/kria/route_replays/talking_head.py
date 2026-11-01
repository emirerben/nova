"""Talking head replay (KRI-1097 wave 2, lane talking), under the `score_route.py` contract.

Calls the route's render entry, `compile_phone_subtitled_plan`, the way the job runner does:
the fixture's own plan and asks as given, the speaker clip from ``clips`` as a phone source
binding, and the writer's text rows from ``replay_inputs.text_elements`` (the spoken-text row
the recorded render drew, rebuilt with `phone_spoken_text.spoken_text_element`). No caption
cues: captions are their own layers and the graded value is the title rows' entrance.
"""

from __future__ import annotations

from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_sources import PhoneSourceBinding


def _binding(clip: dict) -> PhoneSourceBinding:
    analysis = clip.get("analysis") or {}
    return PhoneSourceBinding(
        media_id=clip["id"],
        proxy_path=f"replay/analysis-proxy-ios-{clip['id']}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="0" * 64,
            byte_count=1,
            duration_s=clip["duration_s"],
            width=analysis.get("display_width") or analysis["width"],
            height=analysis["height"],
            has_audio=bool(clip.get("has_audio", True)),
        ),
    )


def recipe(fx: dict):  # noqa: ANN201 - the grader reads the entry's return, not this one
    return compile_phone_subtitled_plan(
        tuple(_binding(clip) for clip in fx["clips"]),
        caption_cues=[],
        text_elements=fx["inputs"]["text_elements"],
        text_elements_user_edited=True,
        strategy=fx["strategy"],
        requirements=fx["requirements"],
    )
