"""nova.video.photo_pacing — pick slot_every_n_beats for a photo/video clip set.

Runs as a lightweight concurrent future alongside the tonemap + text + matcher
streams in `orchestrate_generative_job`.  Input is text-only (no media upload).
Output is a single integer in {2, 4, 6, 8} representing how many beats wide each
slot should be.  Track-agnostic: the beat grid converts the integer to wall-clock
seconds at recipe-generation time.

Design: the agent is strictly advisory.  Any failure (Gemini 429, timeout, malformed
JSON) triggers the deterministic `_photo_pacing_fallback` heuristic instead.  The
caller is responsible for fallback — this module just exposes the agent + input/output.

Zero serial latency: the agent is submitted to the thread pool before any variant
render starts.  Even on slow networks the concurrent tonemap + text stream dominates.
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

_VALID_SLOT_BEATS = frozenset({2, 4, 6, 8})


class PhotoPacingInput(BaseModel):
    n_photos: int
    n_videos: int
    video_durations_s: list[float] = Field(default_factory=list)
    clip_energies: list[float] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


class PhotoPacingOutput(BaseModel):
    slot_every_n_beats: int = 8
    rationale: str = ""


class PhotoPacingAgent(Agent[PhotoPacingInput, PhotoPacingOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.photo_pacing",
        prompt_id="photo_pacing",
        prompt_version="2026-06-07",
        model="gemini-2.5-flash",
        thinking_budget=256,
        enable_clarification_retries=False,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = PhotoPacingInput
    Output = PhotoPacingOutput

    def render_prompt(self, input: PhotoPacingInput) -> str:  # noqa: A002
        import json

        summary = json.dumps(
            {
                "n_photos": input.n_photos,
                "n_videos": input.n_videos,
                "video_durations_s": input.video_durations_s[:10],
                "clip_energies": input.clip_energies[:10],
                "subjects": input.subjects[:5],
            }
        )
        return load_prompt("photo_pacing", clip_set_json=summary)

    def parse(self, raw_text: str, input: PhotoPacingInput) -> PhotoPacingOutput:  # noqa: A002
        import json

        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"photo_pacing: invalid JSON — {exc}") from exc

        raw_n = data.get("slot_every_n_beats", 8)
        try:
            n = int(raw_n)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"photo_pacing: slot_every_n_beats not int — {raw_n!r}") from exc

        # Coerce to the nearest valid value; never reject outright.
        if n not in _VALID_SLOT_BEATS:
            n = min(_VALID_SLOT_BEATS, key=lambda v: abs(v - n))
            log.info("photo_pacing_coerced", raw=raw_n, coerced=n)

        return PhotoPacingOutput(
            slot_every_n_beats=n,
            rationale=str(data.get("rationale", ""))[:500],
        )
