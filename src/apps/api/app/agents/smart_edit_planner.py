"""nova.compose.smart_edit_planner — semantic event decisions for Smart Captions.

The agent sees corrected transcript words, deterministic candidates, and a
bounded allowlist of analyzed visual assets. It can select semantic roles and
asset IDs, but never timing values, coordinates, fonts, colors, gains, paths, or
FFmpeg expressions. The parser validates each proposal independently so one bad
event cannot erase the rest of the plan.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ROLES = {"hook", "context_shift", "list_item", "example", "payoff", "cta"}
_SCENE_TOKENS = {
    "hook_accumulation",
    "single_example",
    "example_pair",
    "fullscreen_cutaway",
    "persistent_badge",
}
_TEXT_TOKENS = {"context_title", "section_heading"}
_BOUNDARY_TOKENS = {"horizontal_motion_blur"}
_SFX_ROLES = {
    "visual_enter_soft",
    "visual_enter_accent",
    "chapter_number_pop",
    "keyword_typewriter_tick",
    "transition_whip",
    "badge_enter",
    "cta_click",
}


class SmartPlannerAsset(BaseModel):
    asset_id: str
    kind: Literal["image", "video"] = "image"
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    brands: list[str] = Field(default_factory=list, max_length=10)
    filename: str = ""
    aspect: float | None = None
    duration_s: float | None = None


class SmartPlannerCandidate(BaseModel):
    candidate_id: str
    role: str
    start_word_id: str
    end_word_id: str
    anchor_word_id: str
    sequence_number: int | None = None
    evidence: str = ""
    suggested_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    suggested_asset_anchor_word_ids: dict[str, str] = Field(default_factory=dict, max_length=5)


class SmartEditPlannerInput(BaseModel):
    words: list[dict] = Field(min_length=1, max_length=600)
    candidates: list[SmartPlannerCandidate] = Field(default_factory=list, max_length=80)
    assets: list[SmartPlannerAsset] = Field(default_factory=list, max_length=20)
    preset_id: str
    preset_version: str
    language: str = ""


class SmartPlannerProposal(BaseModel):
    role: Literal["hook", "context_shift", "list_item", "example", "payoff", "cta"]
    start_word_id: str
    end_word_id: str
    anchor_word_id: str
    confidence_tier: Literal["high", "medium", "low"] = "medium"
    sequence_number: int | None = Field(default=None, ge=1, le=20)
    text_token: str | None = None
    scene_token: str | None = None
    visual_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    visual_anchor_word_ids: dict[str, str] = Field(default_factory=dict, max_length=5)
    sfx_roles: list[str] = Field(default_factory=list, max_length=3)
    boundary_token: str | None = None
    rationale: str = Field(default="", max_length=300)


class SmartEditPlannerOutput(BaseModel):
    proposals: list[SmartPlannerProposal] = Field(default_factory=list, max_length=80)


class SmartEditPlannerAgent(Agent[SmartEditPlannerInput, SmartEditPlannerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.smart_edit_planner",
        prompt_id="smart_edit_planner",
        prompt_version="2026-07-18.2",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = SmartEditPlannerInput
    Output = SmartEditPlannerOutput

    def required_fields(self) -> list[str]:
        return ["proposals"]

    def render_prompt(self, input: SmartEditPlannerInput) -> str:  # noqa: A002
        return load_prompt(
            "smart_edit_planner",
            words_json=json.dumps(input.words, ensure_ascii=False),
            candidates_json=json.dumps(
                [candidate.model_dump(mode="json") for candidate in input.candidates],
                ensure_ascii=False,
            ),
            assets_json=json.dumps(
                [asset.model_dump(mode="json") for asset in input.assets],
                ensure_ascii=False,
            ),
            preset_id=input.preset_id,
            preset_version=input.preset_version,
            language=input.language,
        )

    def parse(
        self,
        raw_text: str,
        input: SmartEditPlannerInput,  # noqa: A002
    ) -> SmartEditPlannerOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"smart_edit_planner: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
            raise SchemaError("smart_edit_planner: response must contain a proposals list")

        known_words = {str(word.get("word_id")) for word in input.words}
        word_order = {str(word.get("word_id")): index for index, word in enumerate(input.words)}
        known_assets = {asset.asset_id for asset in input.assets}
        # The deterministic candidate extractor owns transcript grounding.  The
        # model may decorate those candidates, but it must never create a new
        # span from arbitrary word IDs.  Merely checking that IDs exist is not
        # sufficient: a syntactically valid response can still hallucinate an
        # entirely different story while pointing at real transcript words.
        candidates_by_span = {
            (
                candidate.role,
                candidate.start_word_id,
                candidate.end_word_id,
                candidate.anchor_word_id,
            ): candidate
            for candidate in input.candidates
        }
        proposals: list[SmartPlannerProposal] = []
        for raw in payload["proposals"][:80]:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "")
            start = str(raw.get("start_word_id") or "")
            end = str(raw.get("end_word_id") or "")
            anchor = str(raw.get("anchor_word_id") or "")
            if role not in _ROLES:
                continue
            if any(
                not _WORD_ID_RE.fullmatch(value) or value not in known_words
                for value in (start, end, anchor)
            ):
                continue
            if not word_order[start] <= word_order[anchor] <= word_order[end]:
                continue
            candidate = candidates_by_span.get((role, start, end, anchor))
            if candidate is None:
                continue

            confidence = str(raw.get("confidence_tier") or "medium")
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            text_token = str(raw.get("text_token") or "") or None
            if text_token not in _TEXT_TOKENS:
                text_token = None
            scene_token = str(raw.get("scene_token") or "") or None
            if scene_token not in _SCENE_TOKENS:
                scene_token = None
            boundary_token = str(raw.get("boundary_token") or "") or None
            if boundary_token not in _BOUNDARY_TOKENS:
                boundary_token = None
            visual_asset_ids = [
                str(value)
                for value in (raw.get("visual_asset_ids") or [])
                if str(value) in known_assets
            ][:5]
            visual_anchor_word_ids = {
                asset_id: candidate.suggested_asset_anchor_word_ids.get(
                    asset_id, candidate.anchor_word_id
                )
                for asset_id in visual_asset_ids
            }
            sfx_roles = [
                str(value)
                for value in (raw.get("sfx_roles") or [])
                if str(value) in _SFX_ROLES and _TOKEN_RE.fullmatch(str(value))
            ][:3]
            # Sequence numbers are transcript-derived facts, not creative
            # choices.  A model cannot renumber a spoken list item.
            sequence_number = candidate.sequence_number

            try:
                proposals.append(
                    SmartPlannerProposal(
                        role=role,
                        start_word_id=start,
                        end_word_id=end,
                        anchor_word_id=anchor,
                        confidence_tier=confidence,
                        sequence_number=sequence_number,
                        text_token=text_token,
                        scene_token=scene_token,
                        visual_asset_ids=visual_asset_ids,
                        visual_anchor_word_ids=visual_anchor_word_ids,
                        sfx_roles=sfx_roles,
                        boundary_token=boundary_token,
                        rationale=str(raw.get("rationale") or "")[:300],
                    )
                )
            except Exception:
                continue
        return SmartEditPlannerOutput(proposals=proposals)
