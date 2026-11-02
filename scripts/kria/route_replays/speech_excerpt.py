"""Render replay of the phone spoken-excerpt montage (KRI-1097 wave 2, lane L5).

Calls the route's render entry, `compile_phone_speech_montage_plan`, once with the fixture's own
plan, the way `run_phone_speech_montage_job` calls it after the speech planner's sections are
grounded. The grounded sections and the b-roll come from the fixture's ``replay_inputs`` and
its clips (synthetic portrait originals), never from the plan. Contract: the docstring of
`scripts/kria/score_route.py`.
"""

from __future__ import annotations

from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_speech_montage_plan import (
    PhoneSpeechSection,
    compile_phone_speech_montage_plan,
)
from app.services.phone_sources import PhoneSourceBinding


def _binding(clip, index):
    return PhoneSourceBinding(
        media_id=clip["id"],
        proxy_path=f"users/synthetic/threads/synthetic/analysis-proxy-ios-{clip['id']}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=f"{index + 1:064x}",
            byte_count=1_000_000,
            duration_s=clip["duration_s"],
            width=1080,
            height=1920,
            has_audio=clip.get("has_audio", True),
        ),
    )


def _section(row, bindings):
    if row["kind"] == "montage":
        return PhoneSpeechSection(
            kind="montage", duration_s=row["duration_s"], cut_s=row.get("cut_s")
        )
    return PhoneSpeechSection(
        kind="speech",
        speaker=bindings[row["media_id"]],
        source_start_s=row["source_start_s"],
        source_end_s=row["source_end_s"],
        visual=row.get("visual", "speaker"),
    )


def recipe(fx):
    bindings = {clip["id"]: _binding(clip, i) for i, clip in enumerate(fx["clips"])}
    inputs = fx["inputs"]
    sections = tuple(_section(row, bindings) for row in inputs["sections"])
    broll = tuple(bindings[media_id] for media_id in inputs["broll"])
    return compile_phone_speech_montage_plan(sections, broll, strategy=fx["strategy"])
