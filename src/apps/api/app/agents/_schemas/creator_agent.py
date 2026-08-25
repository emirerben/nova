"""Typed, execution-free contracts for the Main Creator Agent.

The creator agent is allowed to reason about a resolved editing context, but it
must not receive storage capabilities.  Media and catalog references in this
module are therefore opaque identifiers; a worker or route resolves them after
validating the plan and its context hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.agents._schemas.edit_format import EditFormat, RenderProgram

CREATOR_AGENT_SCHEMA_VERSION = 1
MAX_CREATOR_COMMANDS = 4
MAX_CREATOR_MEDIA_REFS = 50
MAX_CREATOR_CATALOG_REFS = 50
MAX_CREATOR_OUTPUT_DURATION_S = 60.0
MAX_CREATOR_REVIEW_EVIDENCE = 12
MAX_CREATOR_REVISION_EVIDENCE_IDS = 8
MAX_CREATOR_WORKSPACE_MEDIA_IDS = 50
MAX_CREATOR_CRAFT_COMMANDS = 3


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


class CreatorTargetPin(_CreatorModel):
    """The immutable target a post-render craft operation must address.

    These are identifiers, not capabilities.  The authenticated execution
    gateway resolves them again and rejects a target whose generation changed.
    """

    expected_manifest_hash: str = Field(min_length=64, max_length=64)
    expected_context_hash: str = Field(min_length=64, max_length=64)
    expected_job_id: str = Field(min_length=1, max_length=160)
    expected_variant_id: str = Field(min_length=1, max_length=160)
    expected_generation_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    expected_ownership_epoch: int = Field(ge=0)

    @field_validator("expected_manifest_hash", "expected_context_hash")
    @classmethod
    def _validate_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)

    @field_validator("expected_job_id", "expected_variant_id", "expected_generation_id")
    @classmethod
    def _validate_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)


class SetCaptionStyleCommand(CreatorTargetPin):
    command: Literal["set_caption_style"]
    caption_style: Literal["sentence", "word"]


class SetTransitionCommand(CreatorTargetPin):
    command: Literal["set_transition"]
    boundary_index: int = Field(ge=0)
    transition: Literal["none", "crossfade", "fade_black", "wipe_left", "wipe_right", "fade_white"]
    duration_s: float = Field(default=0.0, ge=0.0, le=0.3)


class SetLookPresetCommand(CreatorTargetPin):
    command: Literal["set_look_preset"]
    slot_index: int = Field(ge=0)
    look_preset: Literal[
        "none",
        "stadium_diffusion",
        "olive_film",
        "smoky_split_tone",
        "golden_hour",
        "faded_analog",
    ]


class SetMediaOverlayCommand(CreatorTargetPin):
    command: Literal["set_media_overlay"]
    asset_id: str = Field(min_length=1, max_length=160)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0)

    @field_validator("start_s", "end_s")
    @classmethod
    def _validate_finite_timing(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("media overlay timing must be finite")
        return value

    @field_validator("asset_id")
    @classmethod
    def _validate_asset_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="asset_id")

    @model_validator(mode="after")
    def _require_positive_window(self) -> SetMediaOverlayCommand:
        if self.end_s <= self.start_s:
            raise ValueError("media overlay end_s must be greater than start_s")
        return self


class SetLicensedSfxCommand(CreatorTargetPin):
    command: Literal["set_licensed_sfx"]
    sound_effect_id: str = Field(min_length=1, max_length=160)
    at_s: float = Field(ge=0.0, le=60.0)

    @field_validator("sound_effect_id")
    @classmethod
    def _validate_sound_effect_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="sound_effect_id")


class ApplySpeechCutCommand(CreatorTargetPin):
    command: Literal["apply_speech_cut"]
    candidate_id: str = Field(min_length=1, max_length=160)
    # Optional on the broader inert CreatorEditPlan wire shape for backwards
    # compatibility; the executable craft route requires it before staging.
    expected_cut_revision: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("candidate_id")
    @classmethod
    def _validate_candidate_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="candidate_id")


class RemoveOptionalTreatmentCommand(CreatorTargetPin):
    """Remove one already-persisted optional treatment; never add or replace media."""

    command: Literal["remove_optional_treatment"]
    treatment: Literal["media_overlay", "sfx"]
    treatment_id: str | None = Field(default=None, max_length=160)

    @field_validator("treatment_id")
    @classmethod
    def _validate_treatment_id(cls, value: str | None) -> str | None:
        return _opaque_id(value, field_name="treatment_id") if value is not None else None


CreatorCraftCommand: TypeAlias = Annotated[
    SetCaptionStyleCommand
    | SetTransitionCommand
    | SetLookPresetCommand
    | SetMediaOverlayCommand
    | SetLicensedSfxCommand
    | ApplySpeechCutCommand
    | RemoveOptionalTreatmentCommand,
    Field(discriminator="command"),
]
CREATOR_CRAFT_COMMAND_ADAPTER = TypeAdapter(CreatorCraftCommand)

# Core, licensed-SFX, and speech-cut commands may share the existing editor
# receipt. Media overlays use the same receipt but remain overlay-only because
# their full replacement list has a separate owner-validation boundary.
CreatorCoreCraftCommand: TypeAlias = Annotated[
    SetCaptionStyleCommand
    | SetTransitionCommand
    | SetLookPresetCommand
    | SetLicensedSfxCommand
    | ApplySpeechCutCommand
    | RemoveOptionalTreatmentCommand,
    Field(discriminator="command"),
]
CREATOR_CORE_CRAFT_COMMAND_ADAPTER = TypeAdapter(CreatorCoreCraftCommand)

CreatorCraftBundleCommand: TypeAlias = Annotated[
    SetCaptionStyleCommand
    | SetTransitionCommand
    | SetLookPresetCommand
    | SetMediaOverlayCommand
    | SetLicensedSfxCommand
    | ApplySpeechCutCommand
    | RemoveOptionalTreatmentCommand,
    Field(discriminator="command"),
]
CREATOR_CRAFT_BUNDLE_COMMAND_ADAPTER = TypeAdapter(CreatorCraftBundleCommand)


class CreatorCraftBundle(_CreatorModel):
    """One exact-generation, atomic bundle of core craft operations.

    Commands retain their own pins so an extracted command cannot be replayed
    against another target.  The bundle repeats the target as its execution
    envelope and rejects any disagreement before a database lookup or write.
    """

    schema_version: Literal[1] = CREATOR_AGENT_SCHEMA_VERSION
    session_id: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    expected_manifest_hash: str = Field(min_length=64, max_length=64)
    expected_context_hash: str = Field(min_length=64, max_length=64)
    expected_job_id: str = Field(min_length=1, max_length=160)
    expected_variant_id: str = Field(min_length=1, max_length=160)
    expected_generation_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    expected_ownership_epoch: int = Field(ge=0)
    commands: list[CreatorCraftBundleCommand] = Field(
        min_length=1, max_length=MAX_CREATOR_CRAFT_COMMANDS
    )

    @field_validator(
        "session_id",
        "idempotency_key",
        "expected_job_id",
        "expected_variant_id",
        "expected_generation_id",
    )
    @classmethod
    def _validate_bundle_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)

    @field_validator("expected_manifest_hash", "expected_context_hash")
    @classmethod
    def _validate_bundle_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _pin_commands_to_bundle_target(self) -> CreatorCraftBundle:
        command_names = {command.command for command in self.commands}
        if "set_media_overlay" in command_names and len(command_names) != 1:
            raise ValueError("media overlay craft must be the only command in a bundle")
        if "remove_optional_treatment" in command_names and len(command_names) != 1:
            raise ValueError("optional treatment removal must be the only command in a bundle")
        for command in self.commands:
            for field_name in (
                "expected_manifest_hash",
                "expected_context_hash",
                "expected_job_id",
                "expected_variant_id",
                "expected_generation_id",
                "expected_revision",
                "expected_ownership_epoch",
            ):
                if getattr(command, field_name, None) != getattr(self, field_name):
                    raise ValueError(f"command does not pin bundle {field_name}")
        return self


class CreatorReviewEvidence(_CreatorModel):
    """One bounded, timestamped observation from an exact rendered generation."""

    evidence_id: str = Field(min_length=1, max_length=160)
    kind: Literal["visual", "audio", "timing", "caption", "structure"]
    severity: Literal["info", "warning", "critical"] = "warning"
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0)
    observation: str = Field(min_length=1, max_length=500)

    @field_validator("evidence_id")
    @classmethod
    def _validate_evidence_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="evidence_id")

    @model_validator(mode="after")
    def _require_positive_window(self) -> CreatorReviewEvidence:
        if self.end_s <= self.start_s:
            raise ValueError("review evidence end_s must be greater than start_s")
        return self


class CreatorRevisionProposal(_CreatorModel):
    """An inert, creator-confirmable revision suggested by a review."""

    revision_id: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=1000)
    evidence_ids: list[str] = Field(
        default_factory=list, max_length=MAX_CREATOR_REVISION_EVIDENCE_IDS
    )
    strategy: CreativeStrategy | None = None

    @field_validator("revision_id")
    @classmethod
    def _validate_revision_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="revision_id")

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, values: list[str]) -> list[str]:
        normalized = [_opaque_id(value, field_name="evidence_ids") for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidence_ids must not contain duplicates")
        return normalized


class CreatorReviewReceipt(_CreatorModel):
    """Review result pinned to one immutable rendered generation."""

    creator_id: str = Field(min_length=1, max_length=160)
    creator_session_id: str = Field(min_length=1, max_length=160)
    plan_item_id: str = Field(min_length=1, max_length=160)
    ownership_epoch: int = Field(ge=0)
    session_revision: int = Field(ge=0)
    job_id: str = Field(min_length=1, max_length=160)
    variant_id: str = Field(min_length=1, max_length=160)
    render_generation_id: str = Field(min_length=1, max_length=160)
    manifest_hash: str = Field(min_length=64, max_length=64)
    context_hash: str = Field(min_length=64, max_length=64)
    review_mode: Literal["objective", "taste", "mixed"]
    decision: Literal["approve", "revise", "reject", "unavailable"]
    reviewer: Literal["video_quality_grader"] = "video_quality_grader"
    quality_score: float | None = Field(default=None, ge=0.0, le=5.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[CreatorReviewEvidence] = Field(
        default_factory=list, max_length=MAX_CREATOR_REVIEW_EVIDENCE
    )
    proposed_revision: CreatorRevisionProposal | None = None
    reviewed_at: str = Field(min_length=1, max_length=80)

    @field_validator(
        "creator_id",
        "creator_session_id",
        "plan_item_id",
        "job_id",
        "variant_id",
        "render_generation_id",
    )
    @classmethod
    def _validate_target_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)

    @field_validator("manifest_hash", "context_hash")
    @classmethod
    def _validate_review_hashes(cls, value: str, info) -> str:
        return _sha256_hex(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _require_revision_for_revise(self) -> CreatorReviewReceipt:
        if self.decision == "revise" and self.proposed_revision is None:
            raise ValueError("revise reviews require a proposed_revision")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("review evidence_id values must be unique")
        if self.proposed_revision and not set(self.proposed_revision.evidence_ids).issubset(
            evidence_ids
        ):
            raise ValueError("revision evidence_ids must reference review evidence")
        return self


class CreatorWorkspaceRelevanceProposal(_CreatorModel):
    """Inert classification of uploaded media against a creator workspace."""

    proposal_id: str = Field(min_length=1, max_length=160)
    creator_id: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(min_length=1, max_length=160)
    ownership_epoch: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    media_ids: list[str] = Field(min_length=1, max_length=MAX_CREATOR_WORKSPACE_MEDIA_IDS)
    status: Literal["pending", "processing", "ready", "failed", "approved", "rejected"] = "pending"
    relevance: Literal["existing_item", "new_topic", "unmatched"]
    target_plan_item_id: str | None = Field(default=None, max_length=160)
    topic: str | None = Field(default=None, max_length=500)
    rationale: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    proposal_hash: str = Field(min_length=64, max_length=64)

    @field_validator(
        "proposal_id",
        "creator_id",
        "plan_id",
        "idempotency_key",
        "target_plan_item_id",
    )
    @classmethod
    def _validate_workspace_ids(cls, value: str | None, info) -> str | None:
        return _opaque_id(value, field_name=info.field_name) if value is not None else None

    @field_validator("media_ids")
    @classmethod
    def _validate_workspace_media_ids(cls, values: list[str]) -> list[str]:
        normalized = [_opaque_id(value, field_name="media_ids") for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("media_ids must not contain duplicates")
        return normalized

    @field_validator("proposal_hash")
    @classmethod
    def _validate_proposal_hash(cls, value: str) -> str:
        return _sha256_hex(value, field_name="proposal_hash")

    @model_validator(mode="after")
    def _validate_relevance_payload(self) -> CreatorWorkspaceRelevanceProposal:
        if self.relevance == "existing_item" and not self.target_plan_item_id:
            raise ValueError("existing_item proposals require target_plan_item_id")
        if self.relevance == "new_topic" and not self.topic:
            raise ValueError("new_topic proposals require topic")
        if self.relevance != "new_topic" and self.topic is not None:
            raise ValueError("only new_topic proposals may include topic")
        return self


class CreatorWorkspaceRelevanceDecision(_CreatorModel):
    """Explicit user decision for a workspace relevance proposal."""

    proposal_id: str = Field(min_length=1, max_length=160)
    expected_proposal_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["accept_existing", "accept_new_topic", "reject"]
    client_event_id: str = Field(min_length=1, max_length=160)

    @field_validator("proposal_id", "client_event_id")
    @classmethod
    def _validate_decision_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)

    @field_validator("expected_proposal_hash")
    @classmethod
    def _validate_expected_proposal_hash(cls, value: str) -> str:
        return _sha256_hex(value, field_name="expected_proposal_hash")


class CreatorWorkspaceDeliverableReceipt(_CreatorModel):
    """One item-scoped identity in a plan-level workspace poll receipt."""

    plan_item_id: str = Field(min_length=1, max_length=160)
    creator_session_id: str = Field(min_length=1, max_length=160)
    ownership_epoch: int = Field(ge=0)
    status: Literal["pending", "processing", "ready", "failed", "stale"] = "pending"
    job_id: str | None = Field(default=None, max_length=160)
    variant_id: str | None = Field(default=None, max_length=160)
    render_generation_id: str | None = Field(default=None, max_length=160)

    @field_validator(
        "plan_item_id", "creator_session_id", "job_id", "variant_id", "render_generation_id"
    )
    @classmethod
    def _validate_receipt_ids(cls, value: str | None, info) -> str | None:
        return _opaque_id(value, field_name=info.field_name) if value is not None else None


class CreatorWorkspaceReceipt(_CreatorModel):
    """Bounded, inert coordination response for multiple child PlanItems."""

    receipt_id: str = Field(min_length=1, max_length=160)
    creator_id: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(min_length=1, max_length=160)
    ownership_epoch: int = Field(ge=0)
    status: Literal["pending", "processing", "ready", "failed", "stale"] = "pending"
    deliverables: list[CreatorWorkspaceDeliverableReceipt] = Field(
        min_length=1, max_length=MAX_CREATOR_WORKSPACE_MEDIA_IDS
    )
    preference_summary: str | None = Field(default=None, max_length=800)
    style: dict | None = None

    @field_validator("receipt_id", "creator_id", "plan_id")
    @classmethod
    def _validate_receipt_owner_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _require_distinct_deliverables(self) -> CreatorWorkspaceReceipt:
        item_ids = [item.plan_item_id for item in self.deliverables]
        session_ids = [item.creator_session_id for item in self.deliverables]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("workspace receipt deliverables must target distinct plan items")
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("workspace receipt deliverables must retain distinct sessions")
        if any(item.ownership_epoch != self.ownership_epoch for item in self.deliverables):
            raise ValueError("workspace receipt ownership epochs must match")
        return self


class CreatorWorkspacePreferenceSignal(_CreatorModel):
    """Explicit creator-authored feedback; inferred signals are not representable."""

    signal_id: str = Field(min_length=1, max_length=160)
    creator_id: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(min_length=1, max_length=160)
    ownership_epoch: int = Field(ge=0)
    source: Literal["creator_explicit"] = "creator_explicit"
    note: str = Field(min_length=1, max_length=1200)
    style: dict | None = None

    @field_validator("signal_id", "creator_id", "plan_id")
    @classmethod
    def _validate_preference_ids(cls, value: str, info) -> str:
        return _opaque_id(value, field_name=info.field_name)


class CreatorAutomationDecision(_CreatorModel):
    """Deterministic controller output for a possible automatic revision."""

    decision: Literal["eligible", "skip", "blocked"]
    reason_code: str = Field(min_length=1, max_length=80)
    review_generation_id: str = Field(min_length=1, max_length=160)
    opted_in: bool
    review_mode: Literal["objective", "taste", "mixed"]
    confidence: float = Field(ge=0.0, le=1.0)
    current_quality: float | None = Field(default=None, ge=0.0, le=5.0)
    expected_improvement: float | None = Field(default=None, ge=0.0, le=5.0)
    render_budget_remaining: int = Field(ge=0, le=2)
    automatic_revision_count: int = Field(ge=0, le=1)
    allowlist_action: (
        Literal[
            "transition_fallback", "caption_legibility", "remove_optional_treatment", "speech_cut"
        ]
        | None
    ) = None
    command: CreatorCraftCommand | None = None
    proposed_revision: CreatorRevisionProposal | None = None

    @field_validator("review_generation_id")
    @classmethod
    def _validate_review_generation_id(cls, value: str) -> str:
        return _opaque_id(value, field_name="review_generation_id")

    @model_validator(mode="after")
    def _require_revision_for_eligibility(self) -> CreatorAutomationDecision:
        if self.decision == "eligible" and self.proposed_revision is None:
            raise ValueError("eligible automation decisions require a revision")
        return self


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
    | SelectReadyVariantCommand
    | SetCaptionStyleCommand
    | SetTransitionCommand
    | SetLookPresetCommand
    | SetMediaOverlayCommand
    | SetLicensedSfxCommand
    | ApplySpeechCutCommand,
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
            expected_context_hash = getattr(command, "expected_context_hash", None)
            if expected_context_hash is not None and expected_context_hash != self.context_hash:
                raise ValueError("every context-pinned command must pin the plan context_hash")
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
    "ApplySpeechCutCommand",
    "AskUser",
    "CapabilityAvailability",
    "CreatorAutomationDecision",
    "CreatorAgentOutput",
    "CreatorAgentResponse",
    "CreatorCatalogRef",
    "CreatorCommand",
    "CreatorCoreCraftCommand",
    "CreatorCraftBundle",
    "CreatorCraftBundleCommand",
    "CreatorCraftCommand",
    "CREATOR_CORE_CRAFT_COMMAND_ADAPTER",
    "CREATOR_CRAFT_BUNDLE_COMMAND_ADAPTER",
    "CreatorEditSnapshot",
    "CreatorEditPlan",
    "CreatorLimits",
    "CreatorMediaRef",
    "CreatorRevisionProposal",
    "CreatorReviewEvidence",
    "CreatorReviewReceipt",
    "RemoveOptionalTreatmentCommand",
    "CreatorTargetPin",
    "CreatorWorkspaceDeliverableReceipt",
    "CreatorWorkspacePreferenceSignal",
    "CreatorWorkspaceReceipt",
    "CreatorWorkspaceRelevanceDecision",
    "CreatorWorkspaceRelevanceProposal",
    "CreativeStrategy",
    "DispatchRenderCommand",
    "DraftGuidedProposalCommand",
    "ProposeStrategy",
    "ResolvedCreatorManifest",
    "ReviewDecision",
    "SetCaptionStyleCommand",
    "SelectReadyVariantCommand",
    "SetItemIntentCommand",
    "SetLicensedSfxCommand",
    "SetLookPresetCommand",
    "SetMediaOverlayCommand",
    "SetTransitionCommand",
    "canonical_context_hash",
    "canonical_json",
    "canonical_manifest_hash",
]
