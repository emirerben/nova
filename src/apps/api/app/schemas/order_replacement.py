"""Pure server-owned order replacement proof records, independent of runtime imports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
SequenceNumber = Annotated[int, Field(strict=True, ge=1)]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _integer_version(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "version" in value and type(value["version"]) is not int:
            raise ValueError("Order replacement version must be an integer")
        return value


class CheckedOrderMoment(_Record):
    occurrence_id: Identifier
    intent_id: Identifier
    label: Annotated[str, Field(strict=True, min_length=1, max_length=300)]
    source_slot: Identifier
    source_fence_digest: Digest
    phone_binding_digest: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    media_id: Identifier
    kind: Literal["video", "image"]
    gcs_path: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    generation: Identifier
    source_start_s: Seconds | None = None
    source_end_s: Seconds | None = None
    hold_duration_s: Seconds | None = None
    evidence_origin: Literal["grounded", "creator_answer", "selected_image"]
    evidence_ref: Identifier
    output_start_s: Seconds
    output_end_s: Seconds
    duration_s: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    playback_rate: Annotated[float, Field(strict=True, ge=0.25, le=4, allow_inf_nan=False)] = 1.0
    freeze_hold_s: Seconds = 0.0

    @model_validator(mode="after")
    def _window(self) -> CheckedOrderMoment:
        if self.output_end_s <= self.output_start_s:
            raise ValueError("A checked moment requires a positive output interval")
        if any(
            marker in self.gcs_path.lower()
            for marker in ("http://", "https://", "?", "token=", "signature=")
        ):
            raise ValueError("Order sources require storage paths, never signed URLs")
        if self.kind == "video":
            if (
                self.source_start_s is None
                or self.source_end_s is None
                or self.source_end_s <= self.source_start_s
                or self.hold_duration_s is not None
                or self.evidence_origin == "selected_image"
            ):
                raise ValueError("A video moment requires a checked positive source interval")
        elif (
            self.source_start_s is not None
            or self.source_end_s is not None
            or self.hold_duration_s is None
            or self.hold_duration_s <= 0
            or self.evidence_origin != "selected_image"
            or self.playback_rate != 1.0
            or self.freeze_hold_s != 0.0
            or self.duration_s != self.hold_duration_s
        ):
            raise ValueError("A photo requires its identity and positive hold duration")
        return self


class OrderReplacementProposal(_Record):
    version: Literal[1] = 1
    predecessor_requirement_id: Annotated[str, Field(strict=True, min_length=1, max_length=24)]
    predecessor_brief_version: Annotated[int, Field(strict=True, ge=1)]
    predecessor_requirement_digest: Digest
    rejection_question_id: Identifier
    rejection_category_key: Identifier
    rejection_selection_sequence: SequenceNumber
    rejected_intent_id: Identifier
    moments: tuple[CheckedOrderMoment, ...] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def _occurrences(self) -> OrderReplacementProposal:
        ids = [moment.occurrence_id for moment in self.moments]
        if len(ids) != len(set(ids)):
            raise ValueError("Order replacement occurrence IDs must be distinct")
        if any(moment.intent_id == self.rejected_intent_id for moment in self.moments):
            raise ValueError("A rejected intent cannot remain in the replacement")
        return self

    @property
    def input_digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class OrderReplacementProof(_Record):
    version: Literal[1] = 1
    proposal: OrderReplacementProposal
    question_id: Identifier
    question_input_digest: Digest
    question_sequence: SequenceNumber
    selection_sequence: SequenceNumber
    option_key: Literal["accept_sequence"] = "accept_sequence"

    @model_validator(mode="after")
    def _confirmation(self) -> OrderReplacementProof:
        if self.question_input_digest != self.proposal.input_digest:
            raise ValueError("Order replacement question inputs changed")
        if not (
            self.proposal.rejection_selection_sequence
            < self.question_sequence
            < self.selection_sequence
        ):
            raise ValueError("Order replacement confirmation event order is invalid")
        return self
