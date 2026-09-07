"""Semantic grounding hints for the shared narration-label materializer.

The model sees the exact creator request, timed words, analyzed media, and
final timeline.  It chooses semantic anchors only.  It never owns timestamps,
score copy, participant identity, or rendering fields; those are validated by
``app.pipeline.narration_labels``.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.pipeline.prompt_loader import load_prompt


class NarrationAnnotationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_request: str = Field(default="", max_length=CREATOR_REQUEST_MAX_CHARS)
    words: list[dict] = Field(min_length=1, max_length=1200)
    media_analysis: list[dict] = Field(default_factory=list, max_length=300)
    timeline: list[dict] = Field(min_length=1, max_length=300)
    requirements: dict = Field(default_factory=dict)


class NarrationAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["intro", "participant", "score", "topic"]
    start_word_id: str | None = Field(default=None, max_length=80)
    end_word_id: str | None = Field(default=None, max_length=80)
    timeline_id: str | None = Field(default=None, max_length=160)
    asset_id: str | None = Field(default=None, max_length=240)
    text: str = Field(default="", max_length=500)
    confidence: Literal["high", "medium", "low"] = "medium"
    reason: str = Field(default="", max_length=240)
    participant_focus: Literal["single_subject", "multiple_subjects", "unknown"] | None = None
    focus_evidence: str | None = Field(default=None, max_length=240)


class NarrationAnnotationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotations: list[NarrationAnnotation] = Field(default_factory=list, max_length=120)


class NarrationAnnotationAgent(Agent[NarrationAnnotationInput, NarrationAnnotationOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.narration_annotations",
        prompt_id="narration_annotations",
        prompt_version="2026-09-07.2",
        model="gemini-2.5-flash",
        thinking_budget=512,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = NarrationAnnotationInput
    Output = NarrationAnnotationOutput

    def required_fields(self) -> list[str]:
        return ["annotations"]

    def render_prompt(self, input: NarrationAnnotationInput) -> str:  # noqa: A002
        return load_prompt(
            "narration_annotations",
            creator_request=input.creator_request or "(none)",
            words_json=json.dumps(input.words, ensure_ascii=False),
            media_analysis_json=json.dumps(input.media_analysis, ensure_ascii=False),
            timeline_json=json.dumps(input.timeline, ensure_ascii=False),
            requirements_json=json.dumps(input.requirements, ensure_ascii=False),
        )

    def parse(
        self,
        raw_text: str,
        input: NarrationAnnotationInput,  # noqa: A002
    ) -> NarrationAnnotationOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"narration_annotations: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("annotations"), list):
            raise SchemaError("narration_annotations: response must contain an annotations list")

        known_words = {
            str(item.get("word_id"))
            for item in input.words
            if isinstance(item, dict) and str(item.get("word_id") or "")
        }
        known_timeline = {
            str(item.get("timeline_id"))
            for item in input.timeline
            if isinstance(item, dict) and str(item.get("timeline_id") or "")
        }
        known_assets = {
            str(item.get("asset_id"))
            for item in input.media_analysis
            if isinstance(item, dict) and str(item.get("asset_id") or "")
        }
        if len(payload["annotations"]) > 120:
            raise SchemaError("narration_annotations: annotations exceeds the 120-item limit")
        annotations: list[NarrationAnnotation] = []
        allowed_keys = {
            "kind",
            "start_word_id",
            "end_word_id",
            "timeline_id",
            "asset_id",
            "text",
            "confidence",
            "reason",
            "participant_focus",
            "focus_evidence",
        }
        for index, raw in enumerate(payload["annotations"]):
            if not isinstance(raw, dict):
                raise SchemaError(f"narration_annotations: annotation {index} is not an object")
            unknown_keys = set(raw) - allowed_keys
            if unknown_keys:
                names = ", ".join(sorted(map(str, unknown_keys)))
                raise SchemaError(
                    f"narration_annotations: annotation {index} has unknown field(s): {names}"
                )
            if raw.get("kind") not in {
                "intro",
                "participant",
                "score",
                "topic",
            }:
                raise SchemaError(f"narration_annotations: annotation {index} has an unknown kind")
            start = raw.get("start_word_id")
            end = raw.get("end_word_id", start)
            timeline_id = raw.get("timeline_id")
            asset_id = raw.get("asset_id")
            if start is not None and str(start) not in known_words:
                raise SchemaError(
                    f"narration_annotations: annotation {index} references unknown start_word_id"
                )
            if end is not None and str(end) not in known_words:
                raise SchemaError(
                    f"narration_annotations: annotation {index} references unknown end_word_id"
                )
            if timeline_id is not None and str(timeline_id) not in known_timeline:
                raise SchemaError(
                    f"narration_annotations: annotation {index} references unknown timeline_id"
                )
            if asset_id is not None and str(asset_id) not in known_assets:
                raise SchemaError(
                    f"narration_annotations: annotation {index} references unknown asset_id"
                )
            kind = raw["kind"]
            if kind in {"score", "topic"} and (start is None or end is None):
                raise SchemaError(
                    f"narration_annotations: {kind} annotation {index} needs a word anchor"
                )
            if kind == "participant" and timeline_id is None and asset_id is None:
                raise SchemaError(
                    "narration_annotations: "
                    f"participant annotation {index} needs timeline_id or asset_id"
                )
            try:
                annotations.append(
                    NarrationAnnotation.model_validate(
                        {
                            **raw,
                            "start_word_id": str(start) if start is not None else None,
                            "end_word_id": str(end) if end is not None else None,
                            "timeline_id": str(timeline_id) if timeline_id is not None else None,
                            "asset_id": str(asset_id) if asset_id is not None else None,
                        }
                    )
                )
            except ValidationError as exc:
                raise SchemaError(
                    f"narration_annotations: annotation {index} failed schema validation: {exc}"
                ) from exc
        return NarrationAnnotationOutput(annotations=annotations)
