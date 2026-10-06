"""nova.plan.speech_excerpt_planner -- choose spoken excerpts and lay out sections (KRI-282).

Text-only: it reads the creator's request, timed sentence segments of the clips
that contain speech, and a one-line summary of every other clip. It returns an
ordered section plan (``app.schemas.speech_montage``) naming excerpts by quoted
phrase. It never sees timestamps it could invent -- a worker grounds each quote
to real word timings (``app.services.speech_segments.ground_excerpt``) -- and it
never sees media ids, only the short refs it was given.

Runs only behind ``SPEECH_EXCERPT_MONTAGE_ENABLED`` (see
``app.services.speech_montage_planning``).
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.pipeline.prompt_loader import load_prompt
from app.schemas.speech_montage import (
    MAX_SECTIONS,
    MontageSection,
    SpeechMontagePlan,
    SpeechSection,
)

_MAX_SPEECH_CLIPS = 6
_MAX_OTHER_CLIPS = 60
_MAX_SEGMENTS_PER_CLIP = 40


class SpeechClipView(BaseModel):
    ref: str = Field(min_length=1, max_length=20)
    duration_s: float = Field(ge=0)
    to_camera: bool = False
    summary: str = Field(default="", max_length=240)
    language: str = Field(default="", max_length=8)
    segments: list[dict[str, Any]] = Field(default_factory=list, max_length=_MAX_SEGMENTS_PER_CLIP)


class OtherClipView(BaseModel):
    ref: str = Field(min_length=1, max_length=20)
    duration_s: float = Field(ge=0)
    summary: str = Field(default="", max_length=200)


class SpeechExcerptPlannerInput(BaseModel):
    creator_request: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    target_duration_s: float | None = Field(default=None, gt=0)
    # The creator's strategy already chose this clip (by ref) as THE voice.
    voice_clip_ref: str | None = Field(default=None, max_length=20)
    speech_clips: list[SpeechClipView] = Field(default_factory=list, max_length=_MAX_SPEECH_CLIPS)
    other_clips: list[OtherClipView] = Field(default_factory=list, max_length=_MAX_OTHER_CLIPS)


class SpeechExcerptPlannerAgent(Agent[SpeechExcerptPlannerInput, SpeechMontagePlan]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.speech_excerpt_planner",
        prompt_id="speech_excerpt_planner",
        prompt_version="2026-10-06.1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=25.0,
        # Transcripts and the creator's words are private text.
        sensitive_io=True,
    )
    Input = SpeechExcerptPlannerInput
    Output = SpeechMontagePlan

    def required_fields(self) -> list[str]:
        # `wants_speech_excerpts: false` with nothing else is a valid answer.
        return []

    def project_input_for_observability(
        self, input_dict: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(input_dict, dict):
            return None
        request = str(input_dict.get("creator_request") or "")
        return {
            "creator_request": {
                "redacted": True,
                "chars": len(request),
                "sha256": sha256(request.encode()).hexdigest(),
            },
            "speech_clips": len(input_dict.get("speech_clips") or []),
            "other_clips": len(input_dict.get("other_clips") or []),
        }

    def render_prompt(self, input: SpeechExcerptPlannerInput) -> str:  # noqa: A002
        return load_prompt(
            "speech_excerpt_planner",
            creator_request=_defang(input.creator_request),
            target_duration_s=(
                "null" if input.target_duration_s is None else f"{input.target_duration_s:g}"
            ),
            voice_clip=input.voice_clip_ref or "none",
            speech_clips=json.dumps(
                [clip.model_dump() for clip in input.speech_clips], ensure_ascii=False
            ),
            other_clips=json.dumps(
                [clip.model_dump() for clip in input.other_clips], ensure_ascii=False
            ),
            max_sections=str(MAX_SECTIONS),
        )

    def parse(self, raw_text: str, input: SpeechExcerptPlannerInput) -> SpeechMontagePlan:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"speech_excerpt_planner: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("speech_excerpt_planner: response is not a JSON object")
        wants = bool(data.get("wants_speech_excerpts"))
        question = data.get("question")
        raw_sections = data.get("sections") or []
        if not isinstance(raw_sections, list):
            raise SchemaError("speech_excerpt_planner: sections must be a list")
        if not wants:
            return SpeechMontagePlan(wants_speech_excerpts=False)
        if isinstance(question, str) and question.strip():
            if raw_sections:
                raise SchemaError("speech_excerpt_planner: a question cannot accompany sections")
            return SpeechMontagePlan(wants_speech_excerpts=True, question=question)

        speech_refs = {clip.ref for clip in input.speech_clips}
        sections: list[SpeechSection | MontageSection] = []
        seen_quotes: set[tuple[str, str]] = set()
        for index, row in enumerate(raw_sections):
            if not isinstance(row, dict):
                raise SchemaError(f"speech_excerpt_planner: sections[{index}] is not an object")
            kind = row.get("kind")
            try:
                if kind == "speech":
                    section = SpeechSection.model_validate(row)
                elif kind == "montage":
                    section = MontageSection.model_validate(row)
                else:
                    raise SchemaError(
                        f"speech_excerpt_planner: sections[{index}] has unknown kind {kind!r}"
                    )
            except ValidationError as exc:
                raise SchemaError(
                    f"speech_excerpt_planner: invalid section at {index} — {exc}"
                ) from exc
            if isinstance(section, SpeechSection):
                if section.clip_ref not in speech_refs:
                    raise SchemaError(
                        f"speech_excerpt_planner: sections[{index}] clip_ref "
                        f"{section.clip_ref!r} is not a clip with speech"
                    )
                key = (section.clip_ref, section.quote.casefold())
                if key in seen_quotes:
                    continue  # the same excerpt twice is a model stutter, not a request
                seen_quotes.add(key)
            sections.append(section)
        if not any(isinstance(s, SpeechSection) for s in sections):
            raise SchemaError(
                "speech_excerpt_planner: wants_speech_excerpts needs at least one speech section "
                "or a question"
            )
        try:
            return SpeechMontagePlan(wants_speech_excerpts=True, sections=sections[:MAX_SECTIONS])
        except ValidationError as exc:
            raise SchemaError(f"speech_excerpt_planner: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nReturn ONLY the JSON object. Every speech section needs a clip_ref copied "
            "from the clips with speech and a verbatim quote copied from that clip's segments. "
            "Use either sections or a question, never both."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def _defang(value: str) -> str:
    """Same policy as the other text planners: strip control chars, role markers, fences."""
    import re  # noqa: PLC0415

    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    value = re.sub(
        r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]",
        r"\1[role-marker-stripped]",
        value,
    )
    return re.sub(r"```+", "'''", value)


__all__ = [
    "OtherClipView",
    "SpeechClipView",
    "SpeechExcerptPlannerAgent",
    "SpeechExcerptPlannerInput",
]
