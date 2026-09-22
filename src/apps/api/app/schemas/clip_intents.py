"""Generic creator intents over clips, and the grounding fence for labels and
captions (KRI-127, extended by KRI-129).

One open-vocabulary intent replaces the per-feature strategy fields
(``sport_labels``, ``context_label.kind``...): "label / group / order / include
/ caption clips by <creator-described attribute>". A model step resolves an
intent into per-clip assignments with evidence and a confidence; this module
owns the contract and the ONLY rule by which resolved text may reach the
screen.

On-screen text fence (replaces the closed sport allowlist): a label (per-clip
corner text) or a caption (one on-screen phrase for a whole chapter — the
group of clips an intent's membership resolves to) is rendered only when it is

* ``creator_text``     the creator's own words (found in the confirmed request),
* ``record_span``      every word of it appears in what the vision model wrote
                       about the clip (a label: THAT clip; a caption: the
                       UNION of every member clip's record), resolver
                       confidence >= 0.8, or
* ``vision_verified``  the vision model answered it when asked (a label: about
                       THAT clip; a caption: about one representative member
                       clip), confidence >= 0.8.

Anything else is omitted and the creator is asked instead. Spoken transcripts
never ground a label or caption here (speech is third-party text, not vision
evidence).

A caption differs from a label in shape, not in the fence: a label is a <=3
word per-clip tag (``label_text``/``LABEL_MAX_*``); a caption is a <=10 word
phrase for the whole chapter (``clean_caption_text``/``CAPTION_MAX_*``,
``ground_caption``). ``op="caption"`` assignments stay membership-only (like
``group``/``order``/``include`` — ``value`` is always ``None``); the one
authored/quoted caption phrase lives on ``ResolvedClipIntent.caption_text`` +
``caption_grounding`` instead of per-assignment ``value``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.clip_understanding import ClipUnderstanding

ClipIntentOp = Literal["label", "group", "order", "include", "caption"]
ClipOrderPosition = Literal["first", "last"]
GroundingSource = Literal["creator_text", "record_span", "vision_verified"]
ResolutionStatus = Literal["resolved", "needs_creator"]

MAX_CLIP_INTENTS = 6
LABEL_MIN_CONFIDENCE = 0.8
LABEL_MAX_CHARS = 24
LABEL_MAX_WORDS = 3
# Membership (group/order/include/caption) moves clips but prints nothing
# itself, so it needs less certainty than a label's per-clip printed value
# does.
MEMBERSHIP_MIN_CONFIDENCE = 0.6

# A caption is a phrase, not a tag: longer than a label, but still short
# on-screen text for one chapter (never a full sentence).
CAPTION_MAX_CHARS = 60
CAPTION_MAX_WORDS = 10

_LABEL_ALLOWED = re.compile(r"[^\w\s&'\-]", re.UNICODE)
# Captions are ordinary short phrases: allow common sentence punctuation on
# top of the label charset. Newlines can never survive `_clean` (it collapses
# all whitespace, including newlines, to single spaces) so no explicit
# newline check is needed here.
_CAPTION_ALLOWED = re.compile(r"[^\w\s&'\-.,!?:;\"()]", re.UNICODE)


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
    # Transcript labels are resolved only against the pinned narration and final
    # timeline, never by the visual resolver. The kind selects an existing
    # deterministic narration grammar, not model-authored on-screen copy.
    label_source: Literal["clip", "transcript"] = Field(
        default="clip", exclude_if=lambda value: value == "clip"
    )
    transcript_kind: Literal["participant", "score", "topic"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_label_source(self) -> ClipIntent:
        if self.label_source == "transcript":
            if self.op != "label" or self.transcript_kind is None:
                raise ValueError("transcript labels require op=label and transcript_kind")
            if self.creator_text is not None or self.caption_attribute is not None:
                raise ValueError("transcript label copy comes only from the narration materializer")
        elif self.transcript_kind is not None:
            raise ValueError("transcript_kind requires label_source=transcript")
        return self

    # Exact creator-written copy for this intent ("post match pub"), if any.
    creator_text: str | None = Field(default=None, max_length=60)
    # Only for op="caption" with no `creator_text`: what the caption should be
    # ABOUT ("the weather"), as distinct from `attribute` (WHICH clips it's
    # for, "the park clips"). Never set for any other op.
    caption_attribute: str | None = Field(default=None, max_length=160)
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

    @field_validator("caption_attribute", mode="before")
    @classmethod
    def _caption_attribute(cls, v: object) -> str | None:
        return _clean(v, 160) or None


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
    # Only for op="caption": the ONE on-screen phrase for the whole chapter
    # (the member clips in `assignments`) -- `creator_text` verbatim, or the
    # resolver's grounded phrase. `assignments[i].value` stays None for
    # caption, same as group/order/include -- membership only.
    caption_text: str | None = Field(default=None, max_length=CAPTION_MAX_CHARS)
    caption_grounding: GroundingSource | None = None

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


def _word_list(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return [t for t in re.split(r"[^\w]+", plain, flags=re.UNICODE) if t]


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """True when ``phrase``'s words appear in ``haystack`` as WHOLE words, in order,
    contiguously. A letters-only substring test would let "the me" ground "Theme"."""
    words, needle = _word_list(haystack), _word_list(phrase)
    if not needle:
        return False
    span = len(needle)
    return any(words[i : i + span] == needle for i in range(len(words) - span + 1))


def _tokens(text: str) -> set[str]:
    # Every word counts, including one-letter ones: dropping short tokens would
    # let an unverified word ride along inside an otherwise grounded label.
    return set(_word_list(text))


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
    if _contains_phrase(creator_request or "", text):
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


class GroundedCaption(BaseModel):
    """The only shape the caption render lane accepts. Intent-level (one
    caption per chapter), unlike ``GroundedLabel`` which is per-clip."""

    text: str = Field(min_length=1, max_length=CAPTION_MAX_CHARS)
    grounding: GroundingSource
    confidence: float = Field(ge=0.0, le=1.0)
    intent_id: str = ""


def clean_caption_text(value: object) -> str | None:
    """Normalise candidate caption copy; None when it can never be a caption."""
    text = _clean(value, 200)
    if not text or _CAPTION_ALLOWED.search(text):
        return None
    if len(text) > CAPTION_MAX_CHARS or len(text.split()) > CAPTION_MAX_WORDS:
        return None
    if not any(c.isalpha() for c in text):
        return None
    return text


def ground_caption(
    *,
    value: object,
    confidence: float,
    creator_request: str,
    records: list[ClipUnderstanding],
    vision_answer: str | None = None,
    vision_confidence: float | None = None,
    intent_id: str = "",
) -> GroundedCaption | None:
    """Apply the on-screen text fence to an intent-level caption phrase.

    Same three-source fence as ``ground_label``, except ``record_span`` checks
    the caption's words against the UNION of every member clip's vision
    evidence (a caption spans a whole chapter, not one clip) instead of a
    single clip's record.
    """
    text = clean_caption_text(value)
    if text is None:
        return None
    if _contains_phrase(creator_request or "", text):
        return GroundedCaption(
            text=text, grounding="creator_text", confidence=1.0, intent_id=intent_id
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
        return GroundedCaption(
            text=text,
            grounding="vision_verified",
            confidence=float(vision_confidence),
            intent_id=intent_id,
        )
    union_evidence = " ".join(vision_evidence_text(r) for r in records)
    if confidence >= LABEL_MIN_CONFIDENCE and words <= _tokens(union_evidence):
        return GroundedCaption(
            text=text, grounding="record_span", confidence=float(confidence), intent_id=intent_id
        )
    return None
