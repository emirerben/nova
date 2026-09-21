"""Generic creator intents over clips, and the grounding fence for labels (KRI-127).

One open-vocabulary intent replaces the per-feature strategy fields
(``sport_labels``, ``context_label.kind``...): "label / group / order / include
clips by <creator-described attribute>". A model step resolves an intent into
per-clip assignments with evidence and a confidence; this module owns the
contract and the ONLY rule by which resolved text may reach the screen.

On-screen text fence (replaces the closed sport allowlist): a label is rendered
only when it is

* ``creator_text``     the creator's own words (found in the confirmed request),
* ``record_span``      every word of it appears in what the vision model wrote
                       about THAT clip, resolver confidence >= 0.8, or
* ``vision_verified``  the vision model answered it for THAT clip when asked,
                       confidence >= 0.8.

Anything else is omitted and the creator is asked instead. Spoken transcripts
never ground a label here (speech is third-party text, not vision evidence).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.clip_understanding import ClipUnderstanding

ClipIntentOp = Literal["label", "group", "order", "include"]
ClipOrderPosition = Literal["first", "last"]
GroundingSource = Literal["creator_text", "record_span", "vision_verified"]
ResolutionStatus = Literal["resolved", "needs_creator"]

MAX_CLIP_INTENTS = 6
LABEL_MIN_CONFIDENCE = 0.8
LABEL_MAX_CHARS = 24
LABEL_MAX_WORDS = 3
# Membership (group/order/include) moves clips but prints nothing, so it needs
# less certainty than a label does.
MEMBERSHIP_MIN_CONFIDENCE = 0.6

_LABEL_ALLOWED = re.compile(r"[^\w\s&'\-]", re.UNICODE)


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


class ClipIntent(BaseModel):
    """What the creator wants done with clips matching a described attribute."""

    intent_id: str = Field(min_length=1, max_length=40)
    op: ClipIntentOp
    # Free text, the creator's own framing: "sport being played", "pub videos",
    # "people not playing sports", "me talking to the camera", "dish".
    attribute: str = Field(min_length=1, max_length=160)
    # Exact creator-written copy for this intent ("post match pub"), if any.
    creator_text: str | None = Field(default=None, max_length=60)
    # Only for op="order".
    position: ClipOrderPosition | None = None

    @field_validator("attribute", mode="before")
    @classmethod
    def _attribute(cls, v: object) -> str:
        return _clean(v, 160)

    @field_validator("creator_text", mode="before")
    @classmethod
    def _creator_text(cls, v: object) -> str | None:
        return _clean(v, 60) or None


class ClipAssignment(BaseModel):
    media_id: str = Field(min_length=1, max_length=200)
    # Per-clip value for op="label" ("Volleyball"); None for pure membership.
    value: str | None = Field(default=None, max_length=60)
    evidence: str = Field(default="", max_length=240)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    grounding: GroundingSource | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: object) -> str | None:
        return _clean(v, 60) or None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: object) -> str:
        return _clean(v, 240)


class ResolvedClipIntent(ClipIntent):
    status: ResolutionStatus = "resolved"
    assignments: list[ClipAssignment] = Field(default_factory=list)
    # Set when status == "needs_creator": one concise question for the chat.
    question: str | None = Field(default=None, max_length=300)

    def media_ids(self) -> list[str]:
        return [a.media_id for a in self.assignments]


class GroundedLabel(BaseModel):
    """The only shape the context-label render lane accepts when the flag is on."""

    media_id: str
    text: str = Field(min_length=1, max_length=LABEL_MAX_CHARS)
    grounding: GroundingSource
    confidence: float = Field(ge=0.0, le=1.0)
    intent_id: str = ""


def _match_key(text: str) -> str:
    """Accent/case/punctuation-insensitive key (same rule as creator shot labels)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed.casefold() if c.isalnum() and not unicodedata.combining(c))


def _tokens(text: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return {t for t in re.split(r"[^\w]+", plain, flags=re.UNICODE) if len(t) >= 2}


def clean_label_text(value: object) -> str | None:
    """Normalise candidate label copy; None when it can never be a label."""
    text = _clean(value, 200)
    if not text or _LABEL_ALLOWED.search(text):
        return None
    if len(text) > LABEL_MAX_CHARS or len(text.split()) > LABEL_MAX_WORDS:
        return None
    if not any(c.isalpha() for c in text):
        return None
    return text


def vision_evidence_text(record: ClipUnderstanding) -> str:
    """Everything the vision model WROTE about a clip. Never the transcript."""
    parts = [record.subject, record.summary, record.setting, record.activity, record.people.note]
    parts.extend(m.description for m in record.notable_moments)
    return " ".join(p for p in parts if p)


def ground_label(
    *,
    media_id: str,
    value: object,
    confidence: float,
    creator_request: str,
    record: ClipUnderstanding,
    vision_answer: str | None = None,
    vision_confidence: float | None = None,
    intent_id: str = "",
) -> GroundedLabel | None:
    """Apply the on-screen text fence. Returns None when the label must not render."""
    text = clean_label_text(value)
    if text is None:
        return None
    key = _match_key(text)
    if key and key in _match_key(creator_request or ""):
        return GroundedLabel(
            media_id=media_id,
            text=text,
            grounding="creator_text",
            confidence=1.0,
            intent_id=intent_id,
        )
    words = _tokens(text)
    if not words:
        return None
    if (
        vision_answer
        and vision_confidence is not None
        and vision_confidence >= LABEL_MIN_CONFIDENCE
        and words <= _tokens(vision_answer)
    ):
        return GroundedLabel(
            media_id=media_id,
            text=text,
            grounding="vision_verified",
            confidence=float(vision_confidence),
            intent_id=intent_id,
        )
    if confidence >= LABEL_MIN_CONFIDENCE and words <= _tokens(vision_evidence_text(record)):
        return GroundedLabel(
            media_id=media_id,
            text=text,
            grounding="record_span",
            confidence=float(confidence),
            intent_id=intent_id,
        )
    return None
