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
        prompt_version="2026-05-15",
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
        tone_block = f'\nTemplate copy tone: "{input.copy_tone}"' if input.copy_tone else ""
        direction_block = (
            f'\nCreative direction: "{input.creative_direction}"'
            if input.creative_direction
            else ""
        )
        return (
            f"You are ranking the top {input.target_count} moments for a "
            "short-form video. Rank 1 is the hook — the moment that earns the "
            "viewer's first three seconds. Getting it wrong wastes the swipe.\n\n"
            "Make decisions, not suggestions. The downstream matcher trusts "
            "this ordering verbatim — there is no second pass to recover from "
            "a soft rank-1 pick.\n\n"
            "DECISION PRINCIPLES (in priority order):\n\n"
            "  1. RANK-1 HOOK STRENGTH — the rank-1 pick must be the strongest "
            "hook in the candidate set. Use this calibration table:\n"
            "       hook_score 9-10 → instant-curiosity moment, must be ranked 1 "
            "if it exists\n"
            "       hook_score 7-8  → strong; rank 1 only if no 9-10 exists\n"
            "       hook_score 5-6  → middle; rank 2-4 territory\n"
            "       hook_score 3-4  → weak; rank toward the bottom\n"
            "       hook_score <3   → do not rank unless candidate pool forces it\n"
            "     Two candidates tied on hook_score: prefer the one whose "
            "description names a specific action verb (slam, strike, react, "
            "drop) over scene labels (crowd shot, venue interior).\n\n"
            "  2. SET VARIETY — across the top-K, descriptions and energy "
            "levels must span distinct beats:\n"
            "       energy span across the ranked set should be ≥ 2.0 when "
            "the pool allows (don't rank three 7.0-energy moments adjacent "
            "if a 4.5 alternative exists)\n"
            "       no two ranked moments should share the same action verb "
            "or scene noun (run/run, sky/sky) when alternatives exist\n"
            "     If a near-duplicate is forced, prefer the higher hook_score "
            "and flag the duplication in the rationale.\n\n"
            "  3. DESCRIPTION QUALITY — only rank moments whose descriptions "
            "are concrete action verbs, not vague labels. If a candidate's "
            "description is a scene label ('player on field', 'venue interior'), "
            "drop it down the ranking unless its hook_score is so high it "
            "carries the pick alone.\n\n"
            "  4. THEMATIC FIT — when `copy_tone` or `creative_direction` is "
            "provided, the ranking must honor it:\n"
            "       copy_tone=calm    → composed shots, low-mid energy "
            "rank higher than chaos peaks\n"
            "       copy_tone=energetic → high-energy peaks rank higher\n"
            "       copy_tone=formal  → talking-head / composed framing wins ties\n"
            "       copy_tone=casual  → reaction shots / handheld energy wins ties\n"
            "     A pick that contradicts the stated tone must explain itself "
            "in the rationale.\n\n"
            f"  5. TARGET COUNT — return EXACTLY {input.target_count} ranked "
            "entries OR FEWER if the candidate pool is too thin. Returning "
            f"more than {input.target_count} is a contract violation. Padding "
            "with weak picks (hook_score <3) is worse than under-returning.\n\n"
            "RATIONALE FORMAT — every rationale must name the dimension that "
            "decided the rank:\n"
            "  Good:  'hook_score 9.5 — clear winner, action-verb description'\n"
            "  Good:  'energy 8 fits energetic tone; rank 2 over the 9 pick "
            "because description names a specific outcome'\n"
            "  Good:  'rank 4 forced duplicate of rank 2 description; only "
            "alternative was hook_score 2.0'\n"
            "  Bad:   'best moment' / 'good pick' / 'great hook' — boilerplate "
            "rationales are rejected by the eval suite.\n\n"
            f"INPUT — {len(input.candidates)} candidates, target {input.target_count}:\n\n"
            f"Candidates:\n{candidates_block}"
            f"{tone_block}{direction_block}\n\n"
            "OUTPUT — return ONLY valid JSON, no markdown:\n"
            '  {"ranked": [{"id": str, "rank": int (1=best), '
            '"rationale": "<dimension that decided + concrete value>"}, ...]}\n\n'
            "Ranks must be dense from 1. No gaps, no duplicates."
        )

    def parse(self, raw_text: str, input: ShotRankerInput) -> ShotRankerOutput:  # noqa: A002
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
