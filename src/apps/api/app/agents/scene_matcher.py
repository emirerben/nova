"""nova.compose.scene_matcher — the word→visual matching brain for Smart Captions.

The deterministic token-overlap matcher can only pair a visual with a spoken
word when the asset's metadata literally contains that token — "footballer in
an Argentina #10 jersey" never matches the spoken word "Messi", and the bare
token "flag" cross-matches every flag in the pool (the 2026-07-21 "silly
matching" report). This agent reads the word-timed transcript next to the
analyzed asset catalog and decides, with world knowledge, WHICH asset belongs
to WHICH spoken moment. It also tags semantic cues (chapter numbers, context
shifts, payoff, CTA) language-agnostically, replacing the Turkish-only vocab
tables as the primary signal.

Grounding contract (mirrors smart_edit_planner): the agent may only reference
asset IDs from the provided catalog and word IDs from the provided transcript.
Timing, coordinates, and rendering primitives are never model-controlled. The
parser validates every item independently — one bad match cannot erase the
rest — and the planner treats the whole output as advisory with the
deterministic heuristics as fallback.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.smart_edit_planner import SmartPlannerAsset
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")
# Roles the matcher may tag. "hook" is deliberately absent: the first cue is
# always the hook, a transcript-derived fact the model must not reassign.
_TAG_ROLES = {"list_item", "context_shift", "payoff", "cta"}
_MAX_MATCHES = 12
_MAX_TAGS = 30
_MAX_MATCHES_PER_ASSET = 2


class SceneMatch(BaseModel):
    asset_id: str
    anchor_word_id: str
    confidence: Literal["high", "medium"] = "medium"
    reason: str = Field(default="", max_length=200)


class SceneCueTag(BaseModel):
    anchor_word_id: str
    role: Literal["list_item", "context_shift", "payoff", "cta"]
    sequence_number: int | None = Field(default=None, ge=1, le=20)
    reason: str = Field(default="", max_length=200)


class SceneMatcherInput(BaseModel):
    words: list[dict] = Field(min_length=1, max_length=600)
    assets: list[SmartPlannerAsset] = Field(default_factory=list, max_length=20)
    language: str = ""


class SceneMatcherOutput(BaseModel):
    matches: list[SceneMatch] = Field(default_factory=list, max_length=_MAX_MATCHES)
    cue_tags: list[SceneCueTag] = Field(default_factory=list, max_length=_MAX_TAGS)


class SceneMatcherAgent(Agent[SceneMatcherInput, SceneMatcherOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.scene_matcher",
        prompt_id="scene_matcher",
        prompt_version="2026-07-21.1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = SceneMatcherInput
    Output = SceneMatcherOutput

    def required_fields(self) -> list[str]:
        return ["matches"]

    def render_prompt(self, input: SceneMatcherInput) -> str:  # noqa: A002
        return load_prompt(
            "scene_matcher",
            words_json=json.dumps(input.words, ensure_ascii=False),
            assets_json=json.dumps(
                [asset.model_dump(mode="json") for asset in input.assets],
                ensure_ascii=False,
            ),
            language=input.language,
        )

    def parse(
        self,
        raw_text: str,
        input: SceneMatcherInput,  # noqa: A002
    ) -> SceneMatcherOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"scene_matcher: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
            raise SchemaError("scene_matcher: response must contain a matches list")

        known_words = {str(word.get("word_id")) for word in input.words}
        known_assets = {asset.asset_id for asset in input.assets}

        matches: list[SceneMatch] = []
        per_asset: dict[str, int] = {}
        for raw in payload["matches"][: _MAX_MATCHES * 2]:
            if not isinstance(raw, dict):
                continue
            asset_id = str(raw.get("asset_id") or "")
            anchor = str(raw.get("anchor_word_id") or "")
            if asset_id not in known_assets:
                continue
            if not _WORD_ID_RE.fullmatch(anchor) or anchor not in known_words:
                continue
            confidence = str(raw.get("confidence") or "medium")
            if confidence == "low":
                # Low-confidence guesses are exactly the "silly matching" class
                # this agent exists to eliminate — drop rather than render.
                continue
            if confidence not in {"high", "medium"}:
                confidence = "medium"
            if per_asset.get(asset_id, 0) >= _MAX_MATCHES_PER_ASSET:
                continue
            per_asset[asset_id] = per_asset.get(asset_id, 0) + 1
            try:
                matches.append(
                    SceneMatch(
                        asset_id=asset_id,
                        anchor_word_id=anchor,
                        confidence=confidence,
                        reason=str(raw.get("reason") or "")[:200],
                    )
                )
            except Exception:  # noqa: BLE001 — one bad item never erases the rest
                continue
            if len(matches) >= _MAX_MATCHES:
                break

        cue_tags: list[SceneCueTag] = []
        seen_anchor_roles: set[tuple[str, str]] = set()
        seen_sequence_numbers: set[int] = set()
        for raw in (payload.get("cue_tags") or [])[: _MAX_TAGS * 2]:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "")
            anchor = str(raw.get("anchor_word_id") or "")
            if role not in _TAG_ROLES:
                continue
            if not _WORD_ID_RE.fullmatch(anchor) or anchor not in known_words:
                continue
            if (anchor, role) in seen_anchor_roles:
                continue
            sequence_number = raw.get("sequence_number")
            if sequence_number is not None:
                try:
                    sequence_number = int(sequence_number)
                except (TypeError, ValueError):
                    sequence_number = None
            if role == "list_item" and sequence_number is not None:
                if not 1 <= sequence_number <= 20 or sequence_number in seen_sequence_numbers:
                    # A spoken list has exactly one "2" — a duplicate or wild
                    # number is a hallucination, not a chapter.
                    continue
                seen_sequence_numbers.add(sequence_number)
            if role != "list_item":
                sequence_number = None
            seen_anchor_roles.add((anchor, role))
            try:
                cue_tags.append(
                    SceneCueTag(
                        anchor_word_id=anchor,
                        role=role,
                        sequence_number=sequence_number,
                        reason=str(raw.get("reason") or "")[:200],
                    )
                )
            except Exception:  # noqa: BLE001
                continue
            if len(cue_tags) >= _MAX_TAGS:
                break

        return SceneMatcherOutput(matches=matches, cue_tags=cue_tags)
