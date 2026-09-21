"""Shared, open-vocabulary understanding of one creator clip (KRI-127).

The video/image analyzers write this record; the chat agent, the edit planner
and the clip matchers all read the SAME projection via
``app.services.clip_understanding.clip_record``. Every field is free text or a
plain flag on purpose: a new kind of creator request ("name the dish", "label
each city") must not need a new field, enum or keyword list here.

This is AI-written evidence. It is never on-screen copy by itself.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

UNDERSTANDING_KEY = "understanding"

_TEXT_LIMIT = 400
_SHORT_TEXT_LIMIT = 200
_TRANSCRIPT_LIMIT = 1200
_MAX_MOMENTS = 8


# Transcripts and descriptions are third-party text that ends up in agent
# prompts. Defang role markers and code fences alongside the prompts' "DATA,
# never instructions" framing (same policy as clip_plan_matcher._sanitize_text).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]")
_FENCE = re.compile(r"```+")


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL_CHARS.sub(" ", value)
    text = _ROLE_MARKERS.sub(r"\1[role-marker-stripped]", text)
    text = _FENCE.sub("'''", text)
    return " ".join(text.split())[:limit]


class ClipPeople(BaseModel):
    count: int | None = Field(default=None, ge=0, le=99)
    speaks_to_camera: bool = False
    note: str = ""

    @field_validator("count", mode="before")
    @classmethod
    def _count(cls, v: object) -> int | None:
        if isinstance(v, bool) or v is None:
            return None
        try:
            n = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return min(max(n, 0), 99)

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, v: object) -> str:
        return _clean_text(v, _SHORT_TEXT_LIMIT)


class ClipSpeech(BaseModel):
    has_speech: bool = False
    to_camera: bool = False
    transcript: str = ""

    @field_validator("transcript", mode="before")
    @classmethod
    def _transcript(cls, v: object) -> str:
        return _clean_text(v, _TRANSCRIPT_LIMIT)


class ClipMomentNote(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0
    description: str = ""

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, v: object) -> str:
        return _clean_text(v, 160)


class ClipUnderstanding(BaseModel):
    """One clip, as every agent sees it."""

    kind: Literal["video", "image"] = "video"
    subject: str = ""
    summary: str = ""
    setting: str = ""
    activity: str = ""
    people: ClipPeople = Field(default_factory=ClipPeople)
    speech: ClipSpeech = Field(default_factory=ClipSpeech)
    on_screen_text: str = ""
    brands: list[str] = Field(default_factory=list)
    content_type: str = ""
    audio_type: str = ""
    notable_moments: list[ClipMomentNote] = Field(default_factory=list)

    @field_validator("subject", "content_type", "audio_type", mode="before")
    @classmethod
    def _short(cls, v: object) -> str:
        return _clean_text(v, _SHORT_TEXT_LIMIT)

    @field_validator("summary", "setting", "activity", "on_screen_text", mode="before")
    @classmethod
    def _text(cls, v: object) -> str:
        return _clean_text(v, _TEXT_LIMIT)

    @field_validator("brands", mode="before")
    @classmethod
    def _brands(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [b for b in (_clean_text(s, 80) for s in v) if b][:10]

    @field_validator("notable_moments", mode="before")
    @classmethod
    def _moments(cls, v: object) -> list[Any]:
        if not isinstance(v, list):
            return []
        return [m for m in v if isinstance(m, (dict, ClipMomentNote))][:_MAX_MOMENTS]

    def is_empty(self) -> bool:
        return not any(
            (
                self.subject,
                self.summary,
                self.setting,
                self.activity,
                self.speech.transcript,
                self.on_screen_text,
                self.notable_moments,
            )
        )

    def prompt_view(self, *, transcript_chars: int = 400) -> dict[str, Any]:
        """Compact dict for an LLM prompt: empty fields dropped, transcript capped."""
        view: dict[str, Any] = {}
        for key in ("subject", "summary", "setting", "activity", "on_screen_text"):
            value = getattr(self, key)
            if value:
                view[key] = value
        if self.people.count is not None or self.people.speaks_to_camera or self.people.note:
            people: dict[str, Any] = {"speaks_to_camera": self.people.speaks_to_camera}
            if self.people.count is not None:
                people["count"] = self.people.count
            if self.people.note:
                people["note"] = self.people.note
            view["people"] = people
        if self.speech.has_speech or self.speech.transcript:
            speech: dict[str, Any] = {
                "has_speech": self.speech.has_speech or bool(self.speech.transcript),
                "to_camera": self.speech.to_camera,
            }
            if self.speech.transcript and transcript_chars > 0:
                speech["transcript"] = self.speech.transcript[:transcript_chars]
            view["speech"] = speech
        if self.brands:
            view["brands"] = self.brands
        if self.content_type:
            view["content_type"] = self.content_type
        if self.audio_type:
            view["audio_type"] = self.audio_type
        moments = [
            {"start_s": m.start_s, "end_s": m.end_s, "description": m.description}
            for m in self.notable_moments
            if m.description
        ]
        if moments:
            view["notable_moments"] = moments
        return view
