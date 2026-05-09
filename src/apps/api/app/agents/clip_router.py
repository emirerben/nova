"""nova.video.clip_router — assign candidate clips → template slots.

Replaces the greedy tuple-sort in `template_matcher.match` that picks one clip
per slot by `(slot_priority, hook_score, energy)`. The heuristic doesn't see the
slot's `slot_type` ("hook", "broll", "punchline") in context — a high-energy
moment may be miscast in a quiet broll slot, and vice versa.

The agent sees all slots and all candidates at once and produces an assignment
optimizing variety across slots and slot-type fit. No media uploaded —
text-only LLM with structured I/O.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError


class SlotSpec(BaseModel):
    position: int = Field(..., ge=1)
    target_duration_s: float = Field(..., gt=0)
    slot_type: str  # "hook" | "broll" | "punchline" | etc.
    energy: float = 5.0


class CandidateClip(BaseModel):
    id: str
    duration_s: float = Field(..., gt=0)
    energy: float = 5.0
    hook_score: float = 5.0
    description: str = ""


class ClipRouterInput(BaseModel):
    slots: list[SlotSpec] = Field(..., min_length=1)
    candidates: list[CandidateClip] = Field(..., min_length=1)
    copy_tone: str = ""
    creative_direction: str = ""


class SlotAssignment(BaseModel):
    slot_position: int
    candidate_id: str
    rationale: str = ""


class ClipRouterOutput(BaseModel):
    assignments: list[SlotAssignment]


class ClipRouterAgent(Agent[ClipRouterInput, ClipRouterOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.clip_router",
        prompt_id="_inline",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = ClipRouterInput
    Output = ClipRouterOutput

    def required_fields(self) -> list[str]:
        return ["assignments"]

    def render_prompt(self, input: ClipRouterInput) -> str:  # noqa: A002
        slots_block = "\n".join(
            f'  Slot {s.position}: type="{s.slot_type}", '
            f"target_duration={s.target_duration_s:.1f}s, "
            f"energy_target={s.energy:.1f}"
            for s in input.slots
        )
        candidates_block = "\n".join(
            f'  {{"id": "{c.id}", "duration_s": {c.duration_s:.1f}, '
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
            f"Assign exactly one candidate clip to each of the {len(input.slots)} "
            "slots below. Optimize for:\n"
            "  1. Slot-type fit — hook slots get high-hook-score moments, "
            "broll slots get visual variety, punchline slots get high-energy peaks\n"
            "  2. Variety across slots — avoid placing two near-identical moments adjacent\n"
            "  3. Energy match — slot's energy_target should align with candidate's energy\n\n"
            f"Slots:\n{slots_block}\n\n"
            f"Candidates:\n{candidates_block}"
            f"{tone_block}{direction_block}\n\n"
            'Return JSON: {"assignments": [{"slot_position": int, "candidate_id": str, '
            '"rationale": short explanation}, ...]}\n'
            "One assignment per slot. Each candidate may appear at most once. "
            "If you have fewer strong candidates than slots, repeat the strongest "
            "rather than padding with weak ones — but flag the repeat in the rationale. "
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self, raw_text: str, input: ClipRouterInput
    ) -> ClipRouterOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"clip_router: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("clip_router: response is not a JSON object")
        raw = data.get("assignments", [])
        if not isinstance(raw, list):
            raise SchemaError("clip_router: assignments is not a list")

        valid_slots = {s.position for s in input.slots}
        valid_ids = {c.id for c in input.candidates}
        assignments: list[SlotAssignment] = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            try:
                pos = int(a.get("slot_position", 0))
                cid = str(a.get("candidate_id", "") or "")
            except (TypeError, ValueError):
                continue
            if pos not in valid_slots or cid not in valid_ids:
                continue  # drop hallucinated slot/candidate IDs
            try:
                assignments.append(
                    SlotAssignment(
                        slot_position=pos,
                        candidate_id=cid,
                        rationale=str(a.get("rationale", "") or "")[:200],
                    )
                )
            except (ValidationError, TypeError, ValueError):
                continue

        # Validate every slot has exactly one assignment
        assigned_slots = {a.slot_position for a in assignments}
        if assigned_slots != valid_slots:
            missing = sorted(valid_slots - assigned_slots)
            raise SchemaError(f"clip_router: missing assignments for slots {missing}")

        return ClipRouterOutput(assignments=assignments)
