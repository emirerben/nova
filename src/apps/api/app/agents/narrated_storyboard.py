"""Transcript-grounded visual planning for narrated edits.

The model may choose which analyzed clip illustrates a spoken span and propose
one short intro title. It never owns timeline timestamps, participant labels, or
score copy. Timings and scores are resolved from Whisper words at the worker
boundary, which keeps the agent creative while making malformed output harmless.
"""

from __future__ import annotations

import json
import math
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")


class NarratedStoryboardMoment(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    description: str = Field(default="", max_length=240)


class NarratedStoryboardClip(BaseModel):
    clip_id: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=500)
    subject: str = Field(default="", max_length=240)
    transcript: str = Field(default="", max_length=500)
    content_type: str = Field(default="broll", max_length=40)
    duration_s: float | None = Field(default=None, ge=0)
    best_moments: list[NarratedStoryboardMoment] = Field(default_factory=list, max_length=8)


class NarratedStoryboardSegment(BaseModel):
    segment_id: str = Field(min_length=1, max_length=160)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    transcript: str = Field(default="", max_length=500)
    guidance: str = Field(default="", max_length=500)


class NarratedStoryboardInput(BaseModel):
    words: list[dict] = Field(min_length=1, max_length=600)
    segments: list[NarratedStoryboardSegment] = Field(min_length=1, max_length=80)
    clips: list[NarratedStoryboardClip] = Field(min_length=1, max_length=50)
    creator_request: str = Field(default="", max_length=1000)
    language: str = Field(default="", max_length=20)


class NarratedStoryboardMatch(BaseModel):
    segment_id: str = Field(min_length=1, max_length=160)
    clip_id: str = Field(min_length=1, max_length=160)
    confidence: Literal["high", "medium", "low"] = "medium"
    source_start_s: float | None = Field(default=None, ge=0)
    reason: str = Field(default="", max_length=240)


class NarratedStoryboardOverlay(BaseModel):
    kind: Literal["intro", "placeholder", "score"]
    anchor_word_id: str | None = Field(default=None, min_length=7, max_length=7)
    end_word_id: str | None = Field(default=None, min_length=7, max_length=7)
    placeholder_index: int | None = Field(default=None, ge=1, le=50)
    text: str | None = Field(default=None, max_length=80)
    reason: str = Field(default="", max_length=200)


class NarratedStoryboardOutput(BaseModel):
    matches: list[NarratedStoryboardMatch] = Field(default_factory=list, max_length=80)
    overlays: list[NarratedStoryboardOverlay] = Field(default_factory=list, max_length=30)


class NarratedStoryboardAgent(Agent[NarratedStoryboardInput, NarratedStoryboardOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.narrated_storyboard",
        prompt_id="narrated_storyboard",
        prompt_version="2026-09-06.4",
        model="gemini-2.5-flash",
        thinking_budget=512,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = NarratedStoryboardInput
    Output = NarratedStoryboardOutput

    def required_fields(self) -> list[str]:
        return ["matches"]

    def render_prompt(self, input: NarratedStoryboardInput) -> str:  # noqa: A002
        return load_prompt(
            "narrated_storyboard",
            words_json=json.dumps(input.words, ensure_ascii=False),
            segments_json=json.dumps(
                [segment.model_dump(mode="json") for segment in input.segments],
                ensure_ascii=False,
            ),
            clips_json=json.dumps(
                [clip.model_dump(mode="json") for clip in input.clips], ensure_ascii=False
            ),
            creator_request=input.creator_request or "(none)",
            language=input.language or "(unknown)",
        )

    def parse(
        self,
        raw_text: str,
        input: NarratedStoryboardInput,  # noqa: A002
    ) -> NarratedStoryboardOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"narrated_storyboard: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
            raise SchemaError("narrated_storyboard: response must contain a matches list")

        known_words = {str(word.get("word_id")) for word in input.words}
        known_segments = {segment.segment_id for segment in input.segments}
        segment_durations = {
            segment.segment_id: max(0.0, segment.end_s - segment.start_s)
            for segment in input.segments
        }
        known_clips = {clip.clip_id for clip in input.clips}
        clip_durations = {clip.clip_id: float(clip.duration_s or 0.0) for clip in input.clips}
        matches: list[NarratedStoryboardMatch] = []
        for raw in payload["matches"][:80]:
            if not isinstance(raw, dict):
                continue
            segment_id = str(raw.get("segment_id") or "")
            clip_id = str(raw.get("clip_id") or "")
            if segment_id not in known_segments or clip_id not in known_clips:
                continue
            confidence = str(raw.get("confidence") or "medium")
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            raw_source_start = raw.get("source_start_s")
            source_start = None
            if isinstance(raw_source_start, (int, float)) and math.isfinite(
                float(raw_source_start)
            ):
                duration = clip_durations.get(clip_id, 0.0)
                segment_duration = segment_durations.get(segment_id, 0.0)
                source_start = max(
                    0.0,
                    min(float(raw_source_start), max(0.0, duration - segment_duration))
                    if duration > 0
                    else float(raw_source_start),
                )
            matches.append(
                NarratedStoryboardMatch(
                    segment_id=segment_id,
                    clip_id=clip_id,
                    confidence=confidence,
                    source_start_s=source_start,
                    reason=str(raw.get("reason") or "")[:240],
                )
            )

        overlays: list[NarratedStoryboardOverlay] = []
        for raw in (payload.get("overlays") or [])[:30]:
            if not isinstance(raw, dict) or raw.get("kind") not in {
                "intro",
                "placeholder",
                "score",
            }:
                continue
            anchor = raw.get("anchor_word_id")
            end = raw.get("end_word_id")
            if anchor is not None and (
                not _WORD_ID_RE.fullmatch(str(anchor)) or str(anchor) not in known_words
            ):
                continue
            if end is not None and (
                not _WORD_ID_RE.fullmatch(str(end)) or str(end) not in known_words
            ):
                continue
            overlays.append(
                NarratedStoryboardOverlay(
                    kind=raw["kind"],
                    anchor_word_id=str(anchor) if anchor is not None else None,
                    end_word_id=str(end) if end is not None else None,
                    placeholder_index=raw.get("placeholder_index"),
                    text=(
                        " ".join(str(raw.get("text") or "").split())[:80]
                        if raw.get("kind") == "intro"
                        else None
                    ),
                    reason=str(raw.get("reason") or "")[:200],
                )
            )
        return NarratedStoryboardOutput(matches=matches, overlays=overlays)
