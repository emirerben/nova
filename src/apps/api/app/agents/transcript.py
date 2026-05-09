"""nova.audio.transcript — Gemini speech-to-text with timestamped words.

Replaces the Gemini path in `app.pipeline.agents.gemini_analyzer.transcribe`.
Whisper fallback stays in `app.pipeline.transcribe` — that's infrastructure
(audio extraction, OpenAI/local backend selection), not a creative-decision LLM
concern. The orchestrator-level `transcribe()` continues to call this agent for
the Gemini branch, then falls through to Whisper on failure.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class Word(BaseModel):
    text: str
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)


class TranscriptInput(BaseModel):
    file_uri: str
    file_mime: str = "video/mp4"


class TranscriptOutput(BaseModel):
    words: list[Word] = Field(default_factory=list)
    full_text: str = ""
    low_confidence: bool = False


class TranscriptAgent(Agent[TranscriptInput, TranscriptOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.transcript",
        prompt_id="transcribe",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        # Single attempt: the Whisper fallback at the caller layer is the
        # actual reliability mechanism for transcription. Retrying here just
        # delays the fallback. Legacy `transcribe()` had no retry loop either.
        max_attempts=1,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TranscriptInput
    Output = TranscriptOutput

    def media_uri(self, input: TranscriptInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: TranscriptInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        return ["full_text", "words"]

    def render_prompt(self, input: TranscriptInput) -> str:  # noqa: A002, ARG002
        return load_prompt("transcribe")

    def parse(self, raw_text: str, input: TranscriptInput) -> TranscriptOutput:  # noqa: A002, ARG002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"transcript: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("transcript: response is not a JSON object")

        words: list[Word] = []
        for w in data.get("words", []) or []:
            if not isinstance(w, dict):
                continue
            try:
                words.append(
                    Word(
                        text=str(w.get("text", "") or ""),
                        start_s=float(w.get("start_s", 0.0) or 0.0),
                        end_s=float(w.get("end_s", 0.0) or 0.0),
                        confidence=1.0,
                    )
                )
            except (ValidationError, TypeError, ValueError):
                continue

        try:
            return TranscriptOutput(
                words=words,
                full_text=str(data.get("full_text", "") or ""),
                low_confidence=bool(data.get("low_confidence", False)),
            )
        except ValidationError as exc:
            raise SchemaError(f"transcript: output validation — {exc}") from exc
