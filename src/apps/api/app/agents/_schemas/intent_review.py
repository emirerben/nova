"""Closed output contract of the intent review agent (KRI-1171).

The model returns verdicts about server-issued requirement aliases, citing server-issued frame
aliases. There is no free text, no wording, no timing and no candidate: every field is an enum,
a bounded alias or a flag, so nothing the model says can become an executable edit. This module
is also the registry of the review vocabularies that prompts render (`prompt_vocab`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1
MAX_FINDINGS = 12
MAX_EVIDENCE_FRAMES = 3
MAX_FRAMES = 6

VERDICTS = ("met", "unmet", "unverifiable")
CONFIDENCE_LEVELS = ("high", "medium", "low")
DEFECT_KINDS = (
    "wrong_content",
    "copy_mismatch",
    "text_missing",
    "text_cut_off",
    "text_obstructed",
    "order_mismatch",
    "style_mismatch",
    "other",
)
ISSUABLE_KINDS = ("text", "text_sequence", "select", "order", "timing", "style")

Verdict = Literal[*VERDICTS]
Confidence = Literal[*CONFIDENCE_LEVELS]
DefectKind = Literal[*DEFECT_KINDS]

DEFECTS_BY_REQUIREMENT_KIND: dict[str, tuple[str, ...]] = {
    "text": ("copy_mismatch", "text_missing", "text_cut_off", "text_obstructed", "other"),
    "text_sequence": ("copy_mismatch", "text_missing", "text_cut_off", "text_obstructed", "other"),
    "select": ("wrong_content", "other"),
    "order": ("order_mismatch", "wrong_content", "other"),
    "timing": ("text_missing", "text_cut_off", "other"),
    "style": ("style_mismatch", "text_obstructed", "other"),
}

_REQUIREMENT_ALIAS = re.compile(r"^R([1-9][0-9]?)$")
_FRAME_ALIAS = re.compile(r"^f([0-9])$")


def is_requirement_alias(value: str) -> bool:
    match = _REQUIREMENT_ALIAS.match(value)
    return bool(match) and 1 <= int(match[1]) <= MAX_FINDINGS


def is_frame_alias(value: str) -> bool:
    match = _FRAME_ALIAS.match(value)
    return bool(match) and int(match[1]) < MAX_FRAMES


class InvalidReviewOutput(ValueError):
    """The review is void: a false 'everything is there' is worse than no review."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class IntentFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    verdict: Verdict
    defect_kind: DefectKind | None
    evidence_frame_ids: list[str] = Field(max_length=MAX_EVIDENCE_FRAMES)
    confidence: Confidence

    @model_validator(mode="after")
    def _consistent(self) -> IntentFinding:
        if not is_requirement_alias(self.requirement_id):
            raise ValueError("requirement_id is not an issued alias")
        if len(set(self.evidence_frame_ids)) != len(self.evidence_frame_ids) or not all(
            is_frame_alias(frame) for frame in self.evidence_frame_ids
        ):
            raise ValueError("evidence_frame_ids must be unique frame aliases")
        if self.verdict == "unmet":
            if self.defect_kind is None or not self.evidence_frame_ids:
                raise ValueError("an unmet finding needs a defect_kind and a frame")
        elif self.defect_kind is not None:
            raise ValueError("defect_kind is only for unmet findings")
        return self


class IntentReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    findings: list[IntentFinding] = Field(max_length=MAX_FINDINGS)

    @model_validator(mode="after")
    def _one_per_requirement(self) -> IntentReviewOutput:
        ids = [finding.requirement_id for finding in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("at most one finding per requirement")
        return self


def counts_as_met(verdict: str, confidence: str, requirement_kind: str | None = None) -> bool:
    """A `met` the creator may be told was checked.

    A low-confidence reading is the model's own guess, and stills sample instants, so they cannot
    show how long anything stays on screen: neither is a pass to report or to build a verdict on.
    """
    return verdict == "met" and confidence != "low" and requirement_kind != "timing"


def parse_review_output(raw_text: str) -> IntentReviewOutput:
    """Strict parse of the model's JSON; anything else voids the whole review."""
    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        raise InvalidReviewOutput("not_json") from None
    if not isinstance(data, dict):
        raise InvalidReviewOutput("not_an_object")
    try:
        return IntentReviewOutput.model_validate(data)
    except ValidationError as exc:
        reason = "schema"
        for error in exc.errors():
            message = str(error.get("msg", ""))
            if "at most one finding" in message:
                reason = "duplicate_requirement"
            elif "unmet finding needs" in message:
                reason = "unmet_without_evidence"
        raise InvalidReviewOutput(reason) from None


def response_schema(requirement_ids: Sequence[str], frame_ids: Sequence[str]) -> dict[str, Any]:
    """Strict JSON schema sent with the call, closed over exactly the aliases that were issued."""
    if not requirement_ids or not frame_ids:
        raise ValueError("a review needs at least one requirement and one frame")
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "requirement_id",
            "verdict",
            "defect_kind",
            "evidence_frame_ids",
            "confidence",
        ],
        "properties": {
            "requirement_id": {"type": "string", "enum": list(requirement_ids)},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "defect_kind": {"type": ["string", "null"], "enum": [*DEFECT_KINDS, None]},
            "evidence_frame_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(frame_ids)},
            },
            "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "findings"],
        "properties": {
            "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
            "findings": {"type": "array", "items": finding},
        },
    }
