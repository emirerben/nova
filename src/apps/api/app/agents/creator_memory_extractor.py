"""Typed, privacy-safe extraction of durable creator direction.

The agent classifies one bounded creator message against a bounded view of
current memory. It never chooses an owner, scope, or provenance identity. Its
output is an allowlisted operation that the direction service must validate
again at the mutation boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt
from app.services.private_text import normalize_private_text

CREATOR_MEMORY_EXTRACTOR_PROMPT_VERSION = "2026-09-06-v1"

MemoryOperation = Literal["activate_explicit", "suggest", "supersede", "forget", "noop"]
MemoryCategory = Literal["content", "video_style", "stories_pacing", "avoid", "other"]
MemoryEnforcement = Literal["constraint", "default", "advisory"]
StructuredKey = Literal[
    "content_type",
    "tone",
    "pacing",
    "edit_format_mix",
    "font_family",
    "text_color",
    "highlight_color",
    "text_size",
    "text_position",
    "text_alignment",
    "font_cycling",
    "stroke_width",
    "shadow_enabled",
]

_SAFE_LABEL_RE = re.compile(r"^[^\n\r\t<>]{1,80}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MAX_CONTEXT_ITEMS = 20
_MAX_CONTEXT_CHARS = 4_000

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
Label = Annotated[str, Field(min_length=1, max_length=80)]
EditFormat = Literal["montage", "talking_head", "subtitled", "narrated", "music"]


def _clean_private_text(value: str) -> str:
    normalized = normalize_private_text(value)
    return " ".join(normalized.split()).strip()


class CreatorMemoryStructuredValue(BaseModel):
    """One executable value, keyed exactly like the direction registry."""

    model_config = ConfigDict(extra="forbid")

    content_type: list[Label] | None = Field(default=None, min_length=1, max_length=8)
    tone: Label | None = None
    pacing: Literal["slow", "calm", "balanced", "fast", "energetic"] | None = None
    edit_format_mix: dict[EditFormat, Annotated[float, Field(ge=0.0, le=1.0)]] | None = Field(
        default=None, min_length=1, max_length=5
    )
    font_family: Label | None = None
    text_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    text_size: Annotated[int, Field(ge=12, le=240)] | Literal["small", "medium", "large"] | None = (
        None
    )
    text_position: Literal["top", "upper", "center", "lower", "bottom"] | None = None
    text_alignment: Literal["left", "center", "right"] | None = None
    font_cycling: bool | None = None
    stroke_width: Annotated[float, Field(ge=0.0, le=20.0)] | None = None
    shadow_enabled: bool | None = None

    @field_validator("tone", "font_family")
    @classmethod
    def validate_safe_label(cls, value: str | None) -> str | None:
        if value is not None:
            value = _clean_private_text(value)
        if value is not None and not _SAFE_LABEL_RE.fullmatch(value):
            raise ValueError("structured label contains unsafe characters")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_content_labels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [_clean_private_text(label) for label in value]
        if any(not _SAFE_LABEL_RE.fullmatch(label) for label in cleaned):
            raise ValueError("content label contains unsafe characters")
        return cleaned

    @field_validator("text_color", "highlight_color")
    @classmethod
    def normalize_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HEX_COLOR_RE.fullmatch(value):
            raise ValueError("color must be a six-digit hex value")
        return value.upper()

    @model_validator(mode="after")
    def exactly_one_value(self) -> CreatorMemoryStructuredValue:
        if len(self.model_dump(exclude_none=True)) != 1:
            raise ValueError("structured_value must contain exactly one supported key")
        if self.edit_format_mix is not None and sum(self.edit_format_mix.values()) <= 0:
            raise ValueError("edit_format_mix must contain a positive weight")
        return self


class CurrentMemoryItem(BaseModel):
    """Bounded current-state context; provenance and owner ids are excluded."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80)
    normalized_key: StructuredKey | None = None
    instruction: ShortText
    enforcement: MemoryEnforcement
    state: Literal["active", "suggested"] = "active"
    user_locked: bool = False

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        cleaned = _clean_private_text(value)
        if not cleaned:
            raise ValueError("instruction is required")
        return cleaned


class CreatorMemoryExtractorInput(BaseModel):
    """Private data supplied to one extractor invocation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_message: str = Field(min_length=1, max_length=2_000)
    candidate_hint: Literal["explicit", "soft", "ambiguous"] = "ambiguous"
    current_memory: list[CurrentMemoryItem] = Field(
        default_factory=list, max_length=_MAX_CONTEXT_ITEMS
    )

    @field_validator("source_message")
    @classmethod
    def normalize_source_message(cls, value: str) -> str:
        cleaned = _clean_private_text(value)
        if not cleaned:
            raise ValueError("source_message is required")
        return cleaned

    @model_validator(mode="after")
    def bound_context_text(self) -> CreatorMemoryExtractorInput:
        if sum(len(item.instruction) for item in self.current_memory) > _MAX_CONTEXT_CHARS:
            raise ValueError("current memory context is too large")
        return self


class CreatorMemoryExtractorOutput(BaseModel):
    """Strict operation proposal. The service remains the mutation authority."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: MemoryOperation
    instruction: ShortText | None = None
    category: MemoryCategory | None = None
    enforcement: MemoryEnforcement | None = None
    normalized_key: StructuredKey | None = None
    structured_value: CreatorMemoryStructuredValue | None = None
    target_item_id: str | None = Field(default=None, min_length=1, max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: Literal[
        "explicit_durable",
        "soft_preference",
        "contradiction",
        "explicit_revocation",
        "local_only",
        "duplicate",
        "insufficient_evidence",
        "unsafe_or_unsupported",
    ]

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean_private_text(value)
        if not cleaned:
            raise ValueError("instruction cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_operation_shape(self) -> CreatorMemoryExtractorOutput:
        mutation = self.operation in {"activate_explicit", "suggest", "supersede"}
        if mutation and not (self.instruction and self.category and self.enforcement):
            raise ValueError("memory mutation requires instruction, category, and enforcement")
        if not mutation and self.instruction is not None:
            raise ValueError("forget/noop cannot carry an instruction")
        if self.operation in {"supersede", "forget"} and self.target_item_id is None:
            raise ValueError("operation requires target_item_id")
        if self.operation not in {"supersede", "forget"} and self.target_item_id is not None:
            raise ValueError("operation cannot carry target_item_id")
        if self.operation == "noop" and any(
            value is not None
            for value in (
                self.category,
                self.enforcement,
                self.normalized_key,
                self.structured_value,
            )
        ):
            raise ValueError("noop cannot carry mutation fields")
        if self.operation == "forget" and any(
            value is not None
            for value in (
                self.category,
                self.enforcement,
                self.normalized_key,
                self.structured_value,
            )
        ):
            raise ValueError("forget can carry only target_item_id")
        if self.operation == "suggest" and self.enforcement != "advisory":
            raise ValueError("suggestions must be advisory")
        if self.operation in {"activate_explicit", "supersede"} and self.enforcement == "advisory":
            raise ValueError("active explicit operations cannot be advisory")
        if (self.normalized_key is None) != (self.structured_value is None):
            raise ValueError("normalized_key and structured_value must be supplied together")
        if self.structured_value is not None:
            keys = set(self.structured_value.model_dump(exclude_none=True))
            if keys != {self.normalized_key}:
                raise ValueError("structured_value must match normalized_key")
        return self


class CreatorMemoryExtractorAgent(Agent[CreatorMemoryExtractorInput, CreatorMemoryExtractorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.creator_memory_extractor",
        prompt_id="creator_memory_extractor",
        prompt_version=CREATOR_MEMORY_EXTRACTOR_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=30.0,
        thinking_budget=256,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        sensitive_io=True,
    )
    Input = CreatorMemoryExtractorInput
    Output = CreatorMemoryExtractorOutput
    response_json = True
    max_output_tokens = 1_200

    def required_fields(self) -> list[str]:
        return ["operation", "confidence", "reason_code"]

    def render_prompt(self, input: CreatorMemoryExtractorInput) -> str:  # noqa: A002
        current_memory = json.dumps(
            [item.model_dump(mode="json") for item in input.current_memory],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return load_prompt(
            "creator_memory_extractor",
            candidate_hint=input.candidate_hint,
            source_message=input.source_message,
            current_memory=current_memory,
        )

    def parse(
        self,
        raw_text: str,
        input: CreatorMemoryExtractorInput,  # noqa: A002
    ) -> CreatorMemoryExtractorOutput:
        try:
            payload = json.loads(raw_text)
            result = CreatorMemoryExtractorOutput.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise SchemaError("creator_memory_extractor: invalid typed output") from exc
        if input.candidate_hint == "soft" and result.operation in {
            "activate_explicit",
            "supersede",
            "forget",
        }:
            raise SchemaError("creator_memory_extractor: soft evidence cannot mutate active memory")
        current = {item.id: item for item in input.current_memory}
        if result.target_item_id is not None:
            target = current.get(result.target_item_id)
            if target is None:
                raise SchemaError("creator_memory_extractor: target is outside current memory")
            if result.operation == "supersede":
                if target.user_locked:
                    raise SchemaError(
                        "creator_memory_extractor: locked memory cannot be superseded"
                    )
                if target.normalized_key != result.normalized_key:
                    raise SchemaError(
                        "creator_memory_extractor: supersede key does not match target"
                    )
        return result

    def project_input_for_observability(self, input_dict: dict | None) -> dict | None:
        if not isinstance(input_dict, dict):
            return None
        message = str(input_dict.get("source_message") or "")
        rows = input_dict.get("current_memory") or []
        return {
            "source_message": {
                "redacted": True,
                "chars": len(message),
                "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            },
            "candidate_hint": input_dict.get("candidate_hint"),
            "current_memory": [
                {
                    "id": row.get("id"),
                    "normalized_key": row.get("normalized_key"),
                    "state": row.get("state"),
                    "user_locked": row.get("user_locked"),
                }
                for row in rows[:_MAX_CONTEXT_ITEMS]
                if isinstance(row, dict)
            ],
            "current_memory_count": len(rows),
        }

    def project_output_for_observability(self, output_dict: dict | None) -> dict | None:
        if not isinstance(output_dict, dict):
            return None
        return {
            key: output_dict.get(key)
            for key in (
                "operation",
                "category",
                "enforcement",
                "normalized_key",
                "target_item_id",
                "confidence",
                "reason_code",
            )
        }

    def schema_clarification(self) -> str:
        return (
            "\n\nReturn one strict JSON operation using only the documented keys and values. "
            "Do not include markdown or fields outside the schema."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "CREATOR_MEMORY_EXTRACTOR_PROMPT_VERSION",
    "CreatorMemoryExtractorAgent",
    "CreatorMemoryExtractorInput",
    "CreatorMemoryExtractorOutput",
    "CreatorMemoryStructuredValue",
    "CurrentMemoryItem",
]
