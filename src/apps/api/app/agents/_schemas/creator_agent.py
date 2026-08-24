"""Typed, execution-free contracts for the Main Creator Agent.

The creator agent is allowed to reason about a resolved editing context, but it
must not receive storage capabilities.  Media and catalog references in this
module are therefore opaque identifiers; a worker or route resolves them after
validating the plan and its context hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.agents._schemas.edit_format import EditFormat, RenderProgram

CREATOR_AGENT_SCHEMA_VERSION = 1
MAX_CREATOR_COMMANDS = 4
MAX_CREATOR_MEDIA_REFS = 50
MAX_CREATOR_CATALOG_REFS = 50
MAX_CREATOR_OUTPUT_DURATION_S = 60.0


class _CreatorModel(BaseModel):
    """Base class for all agent-facing contracts.

    Strict parsing is intentional: an agent typo must stop at the contract
    boundary instead of being silently carried into a render job.
    """

    model_config = ConfigDict(extra="forbid")


def _opaque_id(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    # A storage path or signed URL is a capability, not an identifier.  Keep
    # those out of both the model and any hash derived from it.
    if "://" in value or value.startswith(("/", "gs:", "s3:")):
        raise ValueError(f"{field_name} must be an opaque identifier")
    return value


def _sha256_hex(value: str, *, field_name: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


class CreatorMediaRef(_CreatorModel):
    """A source-media identity safe to expose to the planner."""

    media_id: str = Field(min_length=1, max_length=160)
    kind: Literal["video", "image", "audio"]
    # This is source duration, not the final-output limit.  Phone footage can
    # legitimately be longer than the sub-60-second rendered deliverable.
    duration_s: float | None = Field(default=None, gt=0.0)
    label: str | None = Field(default=None, max_length=160)

    @field_validator("media_id")
    @classmethod
    def _validate_media_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="media_id")


class CreatorCatalogRef(_CreatorModel):
    """An opaque identity from a server-owned catalog."""

    catalog_id: str = Field(min_length=1, max_length=160)
    kind: Literal["music", "sound_effect", "style", "transition", "visual"]
    label: str | None = Field(default=None, max_length=160)

    @field_validator("catalog_id")
    @classmethod
    def _validate_catalog_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="catalog_id")


class CapabilityAvailability(_CreatorModel):
    """A descriptive capability result, including why a capability is absent."""

    available: bool
    reason_code: str | None = Field(default=None, min_length=1, max_length=80)
    reason: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def _require_reason_when_unavailable(self) -> CapabilityAvailability:
        if not self.available and not self.reason_code:
            raise ValueError("unavailable capabilities require reason_code")
        if self.available and (self.reason_code is not None or self.reason is not None):
            raise ValueError("available capabilities cannot carry an unavailable reason")
        return self


class CreatorLimits(_CreatorModel):
    """Boundaries used by the v1 planner and checked again by execution routes."""

    max_media_refs: int = Field(default=MAX_CREATOR_MEDIA_REFS, ge=1, le=MAX_CREATOR_MEDIA_REFS)
    max_catalog_refs: int = Field(
        default=MAX_CREATOR_CATALOG_REFS, ge=1, le=MAX_CREATOR_CATALOG_REFS
    )
    max_commands: int = Field(default=MAX_CREATOR_COMMANDS, ge=1, le=MAX_CREATOR_COMMANDS)
    max_output_duration_s: float = Field(
        default=MAX_CREATOR_OUTPUT_DURATION_S,
        gt=0.0,
        le=MAX_CREATOR_OUTPUT_DURATION_S,
    )


class CreatorEditSnapshot(_CreatorModel):
    """Opaque, read-only summary of the edit the agent is planning against."""

    revision: int = Field(default=0, ge=0)
    status: Literal["none", "draft", "generating", "ready", "failed"] = "none"
    variant_id: str | None = Field(default=None, max_length=160)
    edit_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("variant_id")
    @classmethod
    def _validate_variant_id(cls, value: str | None) -> str | None:
        return _opaque_id(value, field_name="variant_id") if value is not None else None


class ResolvedCreatorManifest(_CreatorModel):
    """Server-resolved, descriptive context for one creator-agent turn.

    This model intentionally contains no executable operation.  In particular,
    it has no storage paths, URLs, or callable route information.
    """

    schema_version: Literal[1] = CREATOR_AGENT_SCHEMA_VERSION
    item_id: str = Field(min_length=1, max_length=160)
    edit_format: EditFormat
    render_program: RenderProgram
    has_voiceover: bool = False
    current_edit: CreatorEditSnapshot | None = None
    media: list[CreatorMediaRef] = Field(default_factory=list, max_length=MAX_CREATOR_MEDIA_REFS)
    catalog: list[CreatorCatalogRef] = Field(
        default_factory=list, max_length=MAX_CREATOR_CATALOG_REFS
    )
    capabilities: dict[str, CapabilityAvailability] = Field(default_factory=dict)
    limits: CreatorLimits = Field(default_factory=CreatorLimits)
    context_hash: str = Field(min_length=64, max_length=64)
    manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("item_id")
    @classmethod
    def _validate_item_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="item_id")

    @field_validator("context_hash", "manifest_hash")
    @classmethod
    def _validate_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)


CreativeDirection = Literal["guided_story", "fast_montage", "text_explainer", "native"]
CreativePace = Literal["relaxed", "balanced", "fast"]
AudioStrategy = Literal["licensed_music", "original_audio", "voiceover"]
CaptionStyle = Literal["none", "clean", "kinetic", "karaoke", "editorial", "auto"]
OptionalTreatment = Literal["overlays", "sfx", "transitions", "looks"]


class CreativeStrategy(_CreatorModel):
    """Bounded editorial choices selected by the orchestrator."""

    direction: CreativeDirection = "fast_montage"
    edit_format: EditFormat = "montage"
    archetype: EditFormat | None = None
    audio_strategy: AudioStrategy = "licensed_music"
    story_structure: list[str] = Field(default_factory=list, max_length=8)
    caption_style: CaptionStyle = "auto"
    intro_hook: str | None = Field(
        default=None,
        max_length=280,
        description=(
            "Opening concept shown for creative approval; never treated as trusted "
            "or burned verbatim by the renderer"
        ),
    )
    pacing: CreativePace = "balanced"
    render_program: RenderProgram = "guided"
    selected_media_ids: list[str] = Field(default_factory=list, max_length=MAX_CREATOR_MEDIA_REFS)
    optional_treatments: list[OptionalTreatment] = Field(default_factory=list, max_length=4)
    rationale: str = Field(default="", max_length=2000)

    @field_validator("story_structure")
    @classmethod
    def _validate_story_structure(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError("story_structure entries must be 1-120 characters")
        return [value.strip() for value in values]

    @field_validator("selected_media_ids")
    @classmethod
    def _validate_media_ids(cls, values: list[str]) -> list[str]:
        return [_opaque_id(value, field_name="selected_media_ids") for value in values]

    @field_validator("optional_treatments")
    @classmethod
    def _validate_treatments(cls, values: list[OptionalTreatment]) -> list[OptionalTreatment]:
        if len(values) != len(set(values)):
            raise ValueError("optional_treatments must not contain duplicates")
        return values

    @property
    def pace(self) -> CreativePace:
        """Compatibility accessor for callers that used the early v1 draft."""

        return self.pacing


class AskUser(_CreatorModel):
    kind: Literal["ask_user"]
    question: str = Field(min_length=1, max_length=1000)
    reason_code: str = Field(min_length=1, max_length=80)
    options: list[str] = Field(default_factory=list, max_length=8)


class ProposeStrategy(_CreatorModel):
    kind: Literal["propose_strategy"]
    strategy: CreativeStrategy
    summary: str = Field(default="", max_length=1000)


class ReviewDecision(_CreatorModel):
    kind: Literal["review_decision"]
    decision: Literal["approve", "revise", "reject"]
    summary: str = Field(default="", max_length=1000)
    issues: list[str] = Field(default_factory=list, max_length=12)


CreatorAgentOutput: TypeAlias = Annotated[
    AskUser | ProposeStrategy | ReviewDecision,
    Field(discriminator="kind"),
]
CREATOR_AGENT_OUTPUT_ADAPTER = TypeAdapter(CreatorAgentOutput)
# Short aliases make the boundary convenient for route code while retaining
# explicit names in generated documentation.
CreatorAgentResponse = CreatorAgentOutput


class SetItemIntentCommand(_CreatorModel):
    command: Literal["set_item_intent"]
    edit_format: EditFormat
    expected_manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("expected_manifest_hash")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256_hex(value, field_name="expected_manifest_hash")


class DraftGuidedProposalCommand(_CreatorModel):
    command: Literal["draft_guided_proposal"]
    strategy: CreativeStrategy
    expected_manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("expected_manifest_hash")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256_hex(value, field_name="expected_manifest_hash")


class DispatchRenderCommand(_CreatorModel):
    command: Literal["dispatch_render"]
    expected_manifest_hash: str = Field(min_length=64, max_length=64)
    expected_context_hash: str = Field(min_length=64, max_length=64)

    @field_validator("expected_manifest_hash", "expected_context_hash")
    @classmethod
    def _validate_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)


class SelectReadyVariantCommand(_CreatorModel):
    command: Literal["select_ready_variant"]
    variant_id: str = Field(min_length=1, max_length=160)
    expected_manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("variant_id")
    @classmethod
    def _validate_variant_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="variant_id")

    @field_validator("expected_manifest_hash")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256_hex(value, field_name="expected_manifest_hash")


CreatorCommand: TypeAlias = Annotated[
    SetItemIntentCommand
    | DraftGuidedProposalCommand
    | DispatchRenderCommand
    | SelectReadyVariantCommand,
    Field(discriminator="command"),
]
CREATOR_COMMAND_ADAPTER = TypeAdapter(CreatorCommand)


class CreatorEditPlan(_CreatorModel):
    """A bounded, hash-pinned plan; it is still inert until a route executes it."""

    schema_version: Literal[1] = CREATOR_AGENT_SCHEMA_VERSION
    manifest_hash: str = Field(min_length=64, max_length=64)
    context_hash: str = Field(min_length=64, max_length=64)
    strategy: CreativeStrategy
    commands: list[CreatorCommand] = Field(default_factory=list, max_length=MAX_CREATOR_COMMANDS)
    review: ReviewDecision | None = None

    @field_validator("manifest_hash", "context_hash")
    @classmethod
    def _validate_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _pin_commands_to_manifest(self) -> CreatorEditPlan:
        for command in self.commands:
            if command.expected_manifest_hash != self.manifest_hash:
                raise ValueError("every command must pin the plan manifest_hash")
        return self


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _canonical_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_payload(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for context fingerprints."""

    return json.dumps(
        _canonical_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_context_hash(value: Any) -> str:
    """Hash a context payload without depending on dict insertion order."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_manifest_hash(manifest: ResolvedCreatorManifest | Mapping[str, Any]) -> str:
    """Hash manifest content while excluding its self-referential hash field."""

    if isinstance(manifest, ResolvedCreatorManifest):
        payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    else:
        payload = dict(manifest)
        payload.pop("manifest_hash", None)
    return canonical_context_hash(payload)


__all__ = [
    "AskUser",
    "CapabilityAvailability",
    "CreatorAgentOutput",
    "CreatorAgentResponse",
    "CreatorCatalogRef",
    "CreatorCommand",
    "CreatorEditSnapshot",
    "CreatorEditPlan",
    "CreatorLimits",
    "CreatorMediaRef",
    "CreativeStrategy",
    "DispatchRenderCommand",
    "DraftGuidedProposalCommand",
    "ProposeStrategy",
    "ResolvedCreatorManifest",
    "ReviewDecision",
    "SelectReadyVariantCommand",
    "SetItemIntentCommand",
    "canonical_context_hash",
    "canonical_json",
    "canonical_manifest_hash",
]
