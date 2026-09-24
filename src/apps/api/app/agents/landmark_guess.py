"""nova.video.landmark_guess — best-guess named landmark for ONE clip (KRI-189).

Given the clip's frames (a Gemini File API reference, the same multimodal path
as ``clip_question``) plus where it was filmed (a reverse-geocoded place name
and a COARSE lat/lon, both optional), name the landmark or venue the clip most
likely shows ("Rumeli Hisarı", "Galata Bridge"). The caller stores the answer
as a ``ClipFact(kind="landmark", provenance="inferred")`` and uses it directly
(decision D4: no confidence gate) — which is exactly why every inferred name is
surfaced for correction in the plan receipt. An unknown answer is legitimate
and produces no fact.

The model may use world knowledge here (unlike ``clip_question``, which may
only describe pixels): a landmark name is a knowledge lookup keyed by what is
visible and where the phone was.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_NAME_MAX_WORDS = 6
_NAME_MAX_CHARS = 60
_UNKNOWN_VALUES = frozenset({"unknown", "n/a", "none", "unclear", "not sure", "not visible"})


def normalize_landmark_name(value: object) -> str:
    """Collapse whitespace, cap length, fold any "unknown"-shaped answer to ''."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or text.strip(".!? ").casefold() in _UNKNOWN_VALUES:
        return ""
    return " ".join(text.split()[:_NAME_MAX_WORDS])[:_NAME_MAX_CHARS]


class LandmarkGuessInput(BaseModel):
    file_uri: str = Field(min_length=1)
    file_mime: str = "video/mp4"
    # Reverse-geocoded on the phone, e.g. "Arnavutköy, İstanbul, Türkiye". "" = unknown.
    place: str = Field(default="", max_length=240)
    # Coarse (two decimals, about 1 km) coordinates, when the phone sent them.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class LandmarkGuessOutput(BaseModel):
    # "" means "no confident guess" (folds in the model's own "unknown").
    name: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=240)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: object) -> str:
        if not isinstance(v, str):
            return ""
        return " ".join(v.split())[:240]

    def is_unknown(self) -> bool:
        return not self.name


class LandmarkGuessAgent(Agent[LandmarkGuessInput, LandmarkGuessOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.landmark_guess",
        prompt_id="landmark_guess",
        prompt_version="2026-09-24.1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # A knowledge lookup over a few frames: some reasoning, not a lot.
        thinking_budget=512,
        # Best-effort context inside plan preparation: fail fast, never retry long.
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=25.0,
    )
    Input = LandmarkGuessInput
    Output = LandmarkGuessOutput

    def media_uri(self, input: LandmarkGuessInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: LandmarkGuessInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        # "unknown" is a legitimate answer; the caller only cares whether the
        # normalized name is non-empty.
        return []

    def render_prompt(self, input: LandmarkGuessInput) -> str:  # noqa: A002
        if input.lat is not None and input.lon is not None:
            coordinates = f"about {input.lat:.2f}, {input.lon:.2f} (rounded to roughly 1 km)"
        else:
            coordinates = "(not available)"
        return load_prompt(
            "landmark_guess",
            place=input.place or "(not available)",
            coordinates=coordinates,
        )

    def parse(self, raw_text: str, input: LandmarkGuessInput) -> LandmarkGuessOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"landmark_guess: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("landmark_guess: response is not a JSON object")
        name = normalize_landmark_name(data.get("name"))
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not name:
            confidence = 0.0
        try:
            return LandmarkGuessOutput(
                name=name,
                confidence=confidence,
                evidence=str(data.get("evidence", "") or ""),
            )
        except ValidationError as exc:
            raise SchemaError(f"landmark_guess: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY the JSON object described above — no "
            "markdown, no prose. `name` MUST be 6 words or fewer, or the "
            'literal string "unknown" if you cannot tell.'
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "LandmarkGuessAgent",
    "LandmarkGuessInput",
    "LandmarkGuessOutput",
    "normalize_landmark_name",
]
