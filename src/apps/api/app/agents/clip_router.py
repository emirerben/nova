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
        prompt_version="2026-05-15",
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
        tone_block = f'\nTemplate copy tone: "{input.copy_tone}"' if input.copy_tone else ""
        direction_block = (
            f'\nCreative direction: "{input.creative_direction}"'
            if input.creative_direction
            else ""
        )
        return (
            "You are routing candidate clips to template slots for a short-form "
            "video. Each slot has a role; each candidate has a measured energy, a "
            "hook_score, and a description of what's happening. Your job is to "
            "produce an assignment that lands the strongest moments in the "
            "slots that need them most, varies the visual texture across the "
            "video, and leaves an audit trail (rationale) that names the "
            "dimension that decided each pick.\n\n"
            "Make decisions, not suggestions. A wrong assignment is invisible to "
            '"looks sane" eyeballing — a swap between two roughly-similar '
            "candidates can quietly degrade the whole narrative.\n\n"
            "DECISION PRINCIPLES (in priority order):\n\n"
            "  1. SLOT-TYPE FIT — the slot's role dictates what kind of "
            "candidate belongs there. Use this table:\n"
            "       hook         → highest hook_score in the candidate pool\n"
            "       punchline    → highest energy peak\n"
            "       outro        → high energy OR a strong closing beat (descriptor mentions "
            "result/payoff)\n"
            "       reaction     → mid-to-high energy with a clear emotional beat in description\n"
            "       face         → candidate with subject visible / talking-head framing\n"
            "       broll        → mid energy, visual interest, varied from adjacent slots\n"
            "       content      → mid energy, supports the narrative without competing\n"
            "     Other slot_type values: treat as 'content' but flag in rationale.\n\n"
            "  2. ENERGY MATCH — pair each slot's `energy_target` with a "
            "candidate whose `energy` lands within ±1.5. Going outside that band is "
            "allowed, but the rationale must justify it (e.g. 'no candidate at "
            "target 8.0; picked 6.5 over 9.5 because description matches outro beat').\n\n"
            "  3. SEQUENCE VARIETY — adjacent slots must NOT use near-duplicate "
            "candidates when alternatives exist. Two candidates are 'near-duplicate' "
            "if their descriptions share the same action verb (run/run, slam/slam) "
            "or the same scene noun (crowd/crowd, sky/sky). If the candidate pool "
            "is too thin to avoid a duplicate, pick the higher hook_score and flag "
            "it.\n\n"
            "  4. ONE CANDIDATE PER SLOT — every candidate appears at most once "
            "across the full assignment. If candidates < slots, repeat the "
            "strongest and FLAG the repeat in the rationale. Never pad with weak "
            "picks.\n\n"
            "RATIONALE FORMAT — every rationale must name the dimension that "
            "decided the pick:\n"
            "  Good:  'hook_score 8.5 — highest in pool, fits hook role'\n"
            "  Good:  'energy 7.2 matches slot target 7.0; description mentions "
            "ball-strike'\n"
            "  Good:  'only candidate with reaction-shot description; energy "
            "gap (5 vs target 7.5) accepted for slot role'\n"
            "  Bad:   'best fit' / 'good match' / 'fits well' — these are "
            "rejected by the eval suite as boilerplate.\n\n"
            f"INPUT — {len(input.slots)} slots, {len(input.candidates)} "
            "candidates:\n\n"
            f"Slots:\n{slots_block}\n\n"
            f"Candidates:\n{candidates_block}"
            f"{tone_block}{direction_block}\n\n"
            "OUTPUT — return ONLY valid JSON, no markdown:\n"
            '  {"assignments": [{"slot_position": int, "candidate_id": str, '
            '"rationale": "<dimension that decided + concrete value>"}, ...]}\n\n'
            "One assignment per slot. Candidates appear at most once unless "
            "explicitly flagged as a forced repeat in the rationale."
        )

    def parse(self, raw_text: str, input: ClipRouterInput) -> ClipRouterOutput:  # noqa: A002
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
