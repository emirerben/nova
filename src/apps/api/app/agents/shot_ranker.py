"""nova.video.shot_ranker — pick top-K moments by hook + variety + thematic fit.

Replaces the tuple-score heuristic in `app.pipeline.template_matcher` that ranks
candidate moments before slot assignment. The heuristic ranks by
`hook_score * 0.7 + engagement * 0.3` — a fixed formula that doesn't account for
variety (can pick three near-identical moments) or thematic alignment with the
template's stated tone.

The agent sees all candidates at once + the template's `copy_tone` /
`creative_direction`, and picks K with explicit reasoning. No media is uploaded
— this is text-only LLM, fast and cheap.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError


class CandidateMoment(BaseModel):
    id: str  # caller-assigned identifier (e.g., "seg3:moment1")
    start_s: float
    end_s: float
    energy: float = 5.0
    description: str = ""
    hook_score: float = 5.0


class ShotRankerInput(BaseModel):
    candidates: list[CandidateMoment] = Field(..., min_length=1)
    target_count: int = Field(..., ge=1)
    copy_tone: str = ""
    creative_direction: str = ""


class RankedMoment(BaseModel):
    id: str
    rank: int = Field(..., ge=1)
    rationale: str = ""


class ShotRankerOutput(BaseModel):
    ranked: list[RankedMoment]


class ShotRankerAgent(Agent[ShotRankerInput, ShotRankerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.shot_ranker",
        prompt_id="_inline",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = ShotRankerInput
    Output = ShotRankerOutput

    def required_fields(self) -> list[str]:
        return ["ranked"]

    def render_prompt(self, input: ShotRankerInput) -> str:  # noqa: A002
        candidates_block = "\n".join(
            f'  {{"id": "{c.id}", "duration_s": {c.end_s - c.start_s:.1f}, '
            f'"energy": {c.energy:.1f}, "hook_score": {c.hook_score:.1f}, '
            f'"description": "{c.description}"}}'
            for c in input.candidates
        )
        tone_block = (
            f'\nTemplate copy tone: "{input.copy_tone}"' if input.copy_tone else ""
        )
        direction_block = (
            f'\nCreative direction: "{input.creative_direction}"'
            if input.creative_direction
            else ""
        )
        return (
            f"You are picking the BEST {input.target_count} moments for a "
            f"short-form video from the candidates below. Optimize for three "
            f"things in order:\n"
            "  1. Hook strength — first impression that creates curiosity\n"
            "  2. Variety — pick moments that differ in description / action / pace\n"
            "  3. Thematic fit — alignment with the template's stated tone\n\n"
            f"Candidates ({len(input.candidates)}):\n{candidates_block}"
            f"{tone_block}{direction_block}\n\n"
            'Return JSON: {"ranked": [{"id": str, "rank": int (1=best), '
            '"rationale": short explanation}, ...]}\n'
            f"Return EXACTLY {input.target_count} entries. "
            "Do not pad with weak picks; if fewer than the target are strong, "
            "rank only the strong ones. Return ONLY valid JSON, no markdown."
        )

    def parse(
        self, raw_text: str, input: ShotRankerInput
    ) -> ShotRankerOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"shot_ranker: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("shot_ranker: response is not a JSON object")
        ranked_raw = data.get("ranked", [])
        if not isinstance(ranked_raw, list):
            raise SchemaError("shot_ranker: ranked is not a list")

        valid_ids = {c.id for c in input.candidates}
        ranked: list[RankedMoment] = []
        for r in ranked_raw:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id", "") or "")
            if rid not in valid_ids:
                continue  # drop hallucinated IDs silently
            try:
                ranked.append(
                    RankedMoment(
                        id=rid,
                        rank=int(r.get("rank", len(ranked) + 1)),
                        rationale=str(r.get("rationale", "") or "")[:200],
                    )
                )
            except (ValidationError, TypeError, ValueError):
                continue

        if not ranked:
            raise SchemaError("shot_ranker: no valid ranked moments returned")

        # Sort by rank to enforce ordering, then re-number 1..N
        ranked.sort(key=lambda m: m.rank)
        for i, m in enumerate(ranked):
            m.rank = i + 1

        return ShotRankerOutput(ranked=ranked)
