"""nova.compose.camera_emphasis — choose the moments that earn a camera push-in.

ONE LLM call per render. The caller hands over every spoken phrase of the edit
with its timing, plus whatever is known about what is on screen; the agent
returns the SMALL subset of those phrases that carry the point, and the shape
each one deserves (a held zoom-in, or a quick pulse).

Posture — agent proposes, server disposes (mirrors `sfx_placement`):

  * the agent only ever returns a `candidate_index` copied from the supplied
    list, never a time of its own, so it cannot place an effect on a window the
    caller did not offer;
  * everything else it returns (style, strength) is a closed vocabulary;
  * `app/services/camera_emphasis.py` re-grounds, clamps, spaces and caps the
    result, and the deterministic preset picks remain the fallback, so an empty
    or failed response degrades to exactly today's behavior.

Transcript text is DATA: it is wrapped as such in the prompt and nothing the
agent returns can reach the screen (the output carries no text at all).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

CAMERA_EMPHASIS_STYLES = ("zoom_in", "pulse")
CAMERA_EMPHASIS_STRENGTHS = ("subtle", "standard", "strong")
MAX_CAMERA_EMPHASES = 6
_MAX_CANDIDATES = 120
_MAX_VISUAL_NOTES = 20


class CameraEmphasisCandidate(BaseModel):
    """One offered window. `index` is the handle the agent answers with."""

    index: int = Field(ge=0)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = ""
    # Semantic role from the Smart planner when the phrase carries one
    # ("list_item", "section_heading", …); empty for a plain sentence.
    role: str = ""
    # True when the deterministic preset already wanted an accent here.
    preset_pick: bool = False


class CameraEmphasisInput(BaseModel):
    candidates: list[CameraEmphasisCandidate] = Field(min_length=1, max_length=_MAX_CANDIDATES)
    duration_s: float = Field(gt=0)
    # How many effects the caller will actually keep — stated so the agent
    # ranks instead of padding.
    max_effects: int = Field(ge=1, le=MAX_CAMERA_EMPHASES)
    # {"start_s","end_s","description"} notes about what is on screen, when the
    # caller has them. May be empty: speech alone is a valid basis.
    visual_notes: list[dict] = Field(default_factory=list, max_length=_MAX_VISUAL_NOTES)
    language_hint: str = "en"


class RawCameraEmphasis(BaseModel):
    candidate_index: int = Field(ge=0)
    style: str = "zoom_in"
    strength: str = "standard"
    reason: str = ""

    @field_validator("style")
    @classmethod
    def _style(cls, v: str) -> str:
        return v if v in CAMERA_EMPHASIS_STYLES else "zoom_in"

    @field_validator("strength")
    @classmethod
    def _strength(cls, v: str) -> str:
        return v if v in CAMERA_EMPHASIS_STRENGTHS else "standard"


class CameraEmphasisOutput(BaseModel):
    emphases: list[RawCameraEmphasis] = Field(default_factory=list, max_length=MAX_CAMERA_EMPHASES)


class CameraEmphasisAgent(Agent[CameraEmphasisInput, CameraEmphasisOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.camera_emphasis",
        prompt_id="camera_emphasis",
        prompt_version="2026-09-21",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        enable_json_repair=True,
    )
    Input = CameraEmphasisInput
    Output = CameraEmphasisOutput

    def required_fields(self) -> list[str]:
        return ["emphases"]

    def render_prompt(self, input: CameraEmphasisInput) -> str:  # noqa: A002
        candidates_payload = [
            {
                "i": candidate.index,
                "s": round(candidate.start_s, 2),
                "e": round(candidate.end_s, 2),
                "text": _sanitize_text(candidate.text)[:200],
                **({"role": _sanitize_text(candidate.role)[:40]} if candidate.role else {}),
                **({"preset_pick": True} if candidate.preset_pick else {}),
            }
            for candidate in input.candidates
        ]
        visual_payload = [
            {
                "s": round(float(note.get("start_s", 0.0)), 2),
                "e": round(float(note.get("end_s", 0.0)), 2),
                "description": _sanitize_text(str(note.get("description") or ""))[:160],
            }
            for note in input.visual_notes
        ]
        return load_prompt(
            "camera_emphasis",
            candidates_json=json.dumps(candidates_payload, ensure_ascii=False),
            visual_notes_json=json.dumps(visual_payload, ensure_ascii=False),
            duration_s=f"{input.duration_s:.1f}",
            max_effects=str(input.max_effects),
            language_hint=input.language_hint,
        )

    def parse(
        self,
        raw_text: str,
        input: CameraEmphasisInput,  # noqa: A002
    ) -> CameraEmphasisOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"camera_emphasis: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("camera_emphasis: response is not a JSON object")
        rows = data.get("emphases")
        if not isinstance(rows, list):
            raise SchemaError("camera_emphasis: 'emphases' must be a list")
        if len(rows) > MAX_CAMERA_EMPHASES:
            raise SchemaError(
                f"camera_emphasis: at most {MAX_CAMERA_EMPHASES} emphases are allowed"
            )
        valid_indexes = {candidate.index for candidate in input.candidates}
        emphases: list[RawCameraEmphasis] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            try:
                parsed = RawCameraEmphasis.model_validate(item)
            except Exception:  # noqa: BLE001 — malformed row, dropped at parse
                continue
            # An index the caller never offered is a hallucinated window: drop
            # it here rather than letting the service guess what was meant.
            if parsed.candidate_index not in valid_indexes:
                continue
            emphases.append(parsed)
        return CameraEmphasisOutput(emphases=emphases)
