"""nova.audio.beat_aligner — map detected beats to slot timing with snap tolerance.

Rule-based wrapper around the existing density heuristic in `music_recipe.py`.
Why an agent interface for code that doesn't call an LLM? Two reasons:
  1. Uniform contract — every clip_router / orchestrator caller treats every
     decision agent identically (input schema → run → output schema), regardless
     of whether the implementation is rules or an LLM.
  2. Future graceful upgrade — when an LLM replaces the heuristic (e.g., for
     genre-aware beat-snap policy), only this file changes. Callers don't.

Compute returns the slot-aligned beats and per-slot durations. The orchestrator
uses these to assemble clips snapped to beat boundaries.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec


class BeatAlignerInput(BaseModel):
    beat_timestamps_s: list[float] = Field(..., min_length=1)
    best_start_s: float = Field(..., ge=0)
    best_end_s: float = Field(..., gt=0)
    slot_every_n_beats: int = Field(default=8, ge=1)


class AlignedSlot(BaseModel):
    position: int = Field(..., ge=1)
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    duration_s: float = Field(..., gt=0)


class BeatAlignerOutput(BaseModel):
    slots: list[AlignedSlot]
    section_beat_count: int


class BeatAlignerAgent(Agent[BeatAlignerInput, BeatAlignerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.beat_aligner",
        prompt_id="_unused",
        prompt_version="2026-05-09",
        model="rule_based",
    )
    Input = BeatAlignerInput
    Output = BeatAlignerOutput

    def render_prompt(self, input: BeatAlignerInput) -> str:  # noqa: A002, ARG002
        return ""

    def parse(
        self, raw_text: str, input: BeatAlignerInput  # noqa: A002, ARG002
    ) -> BeatAlignerOutput:
        raise NotImplementedError

    def compute(self, input: BeatAlignerInput) -> BeatAlignerOutput:  # noqa: A002
        # Beats inside the configured window (inclusive on both ends).
        window = sorted(
            b for b in input.beat_timestamps_s
            if input.best_start_s <= b <= input.best_end_s
        )

        n = input.slot_every_n_beats
        slots: list[AlignedSlot] = []
        # Every n beats → one slot cut. Same loop shape as
        # `music_recipe.generate_music_recipe`, ensuring 1:1 parity.
        for i in range(0, len(window) - n, n):
            start = window[i] - input.best_start_s
            end = window[i + n] - input.best_start_s
            duration = end - start
            if duration <= 0:
                continue
            slots.append(
                AlignedSlot(
                    position=len(slots) + 1,
                    start_s=round(start, 3),
                    end_s=round(end, 3),
                    duration_s=round(duration, 3),
                )
            )

        return BeatAlignerOutput(slots=slots, section_beat_count=len(window))
