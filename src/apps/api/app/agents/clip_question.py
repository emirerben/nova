"""nova.video.clip_question — ask one short, targeted question about ONE clip
(KRI-127 Lane C).

The vision re-query step: `app.services.clip_intent_resolution` calls this,
per clip, ONLY when the stored, shared clip-understanding record
(`app.services.clip_understanding.clip_record`) cannot answer an open-vocabulary
intent (e.g. the record says "playing a ball game" but the creator asked
"which sport"). It is deliberately narrow — one factual question, one short
answer — never a full re-analysis (that is `clip_metadata`'s job at ingest
time).

MULTIMODAL: takes a Gemini File API reference (`media_uri` / `media_mime`,
mirroring `clip_metadata.ClipMetadataAgent`) — the caller uploads the clip's
video bytes via `gemini_upload_and_wait` and passes the resulting `file_ref.uri`.

Image decision (KRI-127 Lane C): this agent does NOT support inline image
bytes. Vision re-query is video-only; a clip with `kind == "image"` (or a
video clip missing a `gcs_path`) is never queued for vision re-query by
`clip_intent_resolution` — it falls straight through to "unresolved" and
contributes to the creator-facing question instead. Rationale: inline image
analysis in this codebase (`app.tasks.autoplace._analyze_image`) calls the raw
`google.genai` client directly with a resized JPEG part outside the Agent
runtime's `media_uri`/`media_mime` contract (no File API ref exists for a still
image), so wiring it through this agent would need a second media path with no
current caller. Images are a small minority of "clips" in practice
(open-vocabulary intents are almost always about video content); if that
changes, add an `inline_image_bytes: bytes | None` override to `media_uri`'s
sibling hook once a caller needs it.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_ANSWER_MAX_WORDS = 3
_ANSWER_MAX_CHARS = 60
_UNKNOWN_VALUES = frozenset({"unknown", "n/a", "none", "unclear", "not sure", "not visible"})


def _normalize_answer(value: object) -> str:
    """Collapse whitespace, cap at 3 words, and fold any "unknown"-shaped
    answer to the empty string (the caller's single "no answer" signal)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text:
        return ""
    if text.strip(".!? ").casefold() in _UNKNOWN_VALUES:
        return ""
    words = text.split()[:_ANSWER_MAX_WORDS]
    return " ".join(words)[:_ANSWER_MAX_CHARS]


class ClipQuestionInput(BaseModel):
    file_uri: str = Field(min_length=1)
    file_mime: str = "video/mp4"
    # Short, specific, answerable-from-this-clip question (from the resolver's
    # `needs_vision` entry, or a generic fallback built from the intent's
    # `attribute`). Free text — no enum of question "kinds".
    question: str = Field(min_length=1, max_length=200)


class ClipQuestionOutput(BaseModel):
    # "" means "no confident answer" (folds in the model's own "unknown").
    answer: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=240)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: object) -> str:
        if not isinstance(v, str):
            return ""
        return " ".join(v.split())[:240]

    def is_unknown(self) -> bool:
        return not self.answer


class ClipQuestionAgent(Agent[ClipQuestionInput, ClipQuestionOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.clip_question",
        prompt_id="clip_question",
        prompt_version="2026-09-21.3",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # One short factual answer — no reasoning depth needed. Kept small so
        # the capped, deadline-bounded chat-turn re-query stays fast.
        thinking_budget=128,
        # Runs inside a chat turn under one 25s batch deadline. asyncio cannot
        # cancel a worker thread, so the agent itself must be short-lived: the
        # default 5 attempts x 30s + backoff (~249s) would keep a Gemini slot
        # and keep billing long after the turn answered the creator.
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=12.0,
    )
    Input = ClipQuestionInput
    Output = ClipQuestionOutput

    def media_uri(self, input: ClipQuestionInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: ClipQuestionInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        # `answer` is intentionally NOT required: "unknown" is a legitimate,
        # expected response (the model saying "I can't tell from this clip"),
        # and required_fields() treats an empty string as a refusal. The
        # caller only cares whether it ends up non-empty after normalization.
        return []

    def render_prompt(self, input: ClipQuestionInput) -> str:  # noqa: A002
        return load_prompt("clip_question", question=input.question)

    def parse(self, raw_text: str, input: ClipQuestionInput) -> ClipQuestionOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"clip_question: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("clip_question: response is not a JSON object")

        answer = _normalize_answer(data.get("answer"))
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not answer:
            # An unknown answer carries no confidence — don't let a stray
            # high-confidence number on an empty answer leak through and be
            # mistaken for a real result by a caller only checking confidence.
            confidence = 0.0

        try:
            return ClipQuestionOutput(
                answer=answer,
                confidence=confidence,
                evidence=str(data.get("evidence", "") or ""),
            )
        except ValidationError as exc:
            raise SchemaError(f"clip_question: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY the JSON object described above — no "
            "markdown, no prose. `answer` MUST be 3 words or fewer, or the "
            'literal string "unknown" if you cannot tell from this clip.'
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = ["ClipQuestionAgent", "ClipQuestionInput", "ClipQuestionOutput"]
