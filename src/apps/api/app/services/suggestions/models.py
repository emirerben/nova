"""Suggestion blocks: the registry row, the Action union, the set and the agent contract.

One registry row per suggestion kind (``SuggestionKind``). **The agent picks and the server
builds**: ``SuggestionAgentOutput`` is the only thing a model returns (kind, target handles,
a reason, optional copy, a rank). The server turns a pick into an ``Action`` through the kind's
``build_action`` and proves it with the kind's ``witness`` before it can reach a phone, so the
card, the chat text and the executed change cannot drift apart.

Wire additions on ``SuggestionSetOut`` (``suggestion_chips.py``) are additive: old iOS builds and
the web client ignore them, and the set's legacy ``chips`` are untouched. The ``Action`` itself
never goes on the wire: it stays server-side (the offered-set ring) and a tap pins it by
``{set_id, id}``.

Nothing here may import ``suggestion_chips``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from app.services.suggestions.context import SuggestionContext, Target

MAX_SUGGESTIONS = 5
MAX_TARGETS = 3
MAX_TITLE_CHARS = 40
MAX_WHY_CHARS = 96
MAX_PROMPT_CHARS = 120
MAX_COPY_CHARS = 200
MAX_EVIDENCE = 3
MAX_EVIDENCE_CHARS = 48
# The agent's handle for "the whole video"; a whole-video kind has no clip to point at.
WHOLE_VIDEO_HANDLE = "all"

Surface = Literal["phone", "cloud"]
SURFACES: frozenset[str] = frozenset({"phone", "cloud"})
DryRunMode = Literal["none", "strategy", "editor", "allowlist"]
WitnessTier = Literal["strategy", "editor", "policy_only", "brief", "setting", "copy", "none"]

# The item fields a suggestion may set. Closed on purpose: an agent pick can never name a field.
ITEM_SETTING_FIELDS = frozenset({"speech_cleanup_enabled"})


class ChipKind(StrEnum):
    """The closed wire enum of the KRI-756 chip kinds. New kinds are registry rows (a ``str``
    kind) and never join this enum: the legacy engine iterates exactly these."""

    DEFAULT_EDIT = "default_edit"
    HOOK = "hook"
    CAPTIONS = "captions"
    MUSIC = "music"
    SFX = "sfx"
    TRANSITION = "transition"
    TITLE_ANIMATION = "title_animation"
    PINNED_TEXT = "pinned_text"
    OPENER_CLIP = "opener_clip"


# ── Wire pieces shared by the legacy chips and the v2 suggestions ─────────────────────


class SuggestionAnchorOut(BaseModel):
    media_digest: str = ""
    manifest_hash: str | None = None
    render_generation_id: str | None = None
    variant_id: str | None = None


class ChipMomentOut(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0


class ChipAnchorOut(BaseModel):
    media_id: str | None = None
    clip_ordinal: int | None = None
    moment: ChipMomentOut | None = None
    text_bar_index: int | None = None


class SuggestionDigest(BaseModel):
    """What a set was computed from. A card is stale the moment any of these moves."""

    media_digest: str = ""
    # Head draft snapshot hash (``CreatorEditDraft.snapshot_hash``); None before any draft.
    snapshot_hash: str | None = None
    brief_version: int | None = None
    signals_version: str = ""
    state: str = "none"
    language: str = "en"
    prompt_version: str = ""

    def value(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]


# ── The Action union (server-side; never serialized to a client) ──────────────────────


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EditorOpsAction(_Action):
    """Editor ops the copilot parser and the draft compiler already accept.

    ``base_digest`` is ``context.snapshot_digest`` of the editor snapshot the witness ran on.
    """

    type: Literal["editor_ops"] = "editor_ops"
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=8)
    base_digest: str = Field(min_length=1, max_length=64)


class BriefUpdateAction(_Action):
    """Typed Creative Brief updates (``app.kria.brief.BriefUpdate``), bound to a brief version."""

    type: Literal["brief_update"] = "brief_update"
    updates: list[dict[str, Any]] = Field(min_length=1, max_length=4)
    expected_version: int = Field(ge=0)

    @field_validator("updates")
    @classmethod
    def _typed(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Lazy: the brief module pulls the ORM, and this package must stay import-light.
        from app.kria.brief import BriefUpdate  # noqa: PLC0415

        return [BriefUpdate.model_validate(item).model_dump(mode="json") for item in value]


class ItemSettingAction(_Action):
    """A plan-item setting from ``ITEM_SETTING_FIELDS`` (then a re-render)."""

    type: Literal["item_setting"] = "item_setting"
    field: str
    value: bool

    @field_validator("field")
    @classmethod
    def _closed(cls, value: str) -> str:
        if value not in ITEM_SETTING_FIELDS:
            raise ValueError("field is not a settable item field")
        return value


class CopyDecisionAction(_Action):
    """Exact on-screen copy the creator approves by tapping. Recorded as ``source="suggestion"``."""

    type: Literal["copy_decision"] = "copy_decision"
    role: Literal["title", "hook"]
    text: str = Field(min_length=1, max_length=MAX_COPY_CHARS)

    @field_validator("text")
    @classmethod
    def _one_line(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("text is empty")
        return text


class AsyncGenerateAction(_Action):
    """Generated media, produced only on tap; the card shows ``cost_usd`` first."""

    type: Literal["async_generate"] = "async_generate"
    spec: dict[str, Any]
    cost_usd: float = Field(ge=0.0, le=2.0)


Action = Annotated[
    EditorOpsAction
    | BriefUpdateAction
    | ItemSettingAction
    | CopyDecisionAction
    | AsyncGenerateAction,
    Field(discriminator="type"),
]
ACTION_TYPES = ("editor_ops", "brief_update", "item_setting", "copy_decision", "async_generate")


# ── Witness + registry row ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WitnessResult:
    """What a witness proved. ``touched`` feeds the engine's edit-target dedupe."""

    ok: bool
    tier: str = "none"
    touched: frozenset[str] = frozenset()
    reason: str | None = None


BuildAction = Callable[["SuggestionContext", Sequence["Target"], "str | None"], "Action | None"]
Witness = Callable[["Action", "SuggestionContext"], WitnessResult]


@dataclass(frozen=True)
class ChipKindSpec:
    """The KRI-756 engine row, ported verbatim. A test fails if any ``ChipKind`` lacks one."""

    kind: ChipKind
    states: frozenset[str]
    capability: Mapping[str, str]  # state -> manifest capability key / editor op family
    available: Callable[[SuggestionContext], bool]
    already_present: Callable[[SuggestionContext], bool]
    ground: Callable[[SuggestionContext], list[Target]]
    dry_run: Mapping[str, DryRunMode]  # state -> how executability is proven
    expects: str  # what a successful tap must change (the eval lane asserts this)
    op: Callable[[SuggestionContext, Target], dict[str, Any] | None] | None = None
    # The editor op a tap is expected to cause (None: routes into a flow, not one op) and the
    # op the dry-run probes with. Guarded against CHIP_OP_ALLOW / CHIP_OP_WITNESS_PROBES.
    claimed_op: str | None = None
    witness_op: str | None = None
    # Lowest iOS protocol that can execute the chip; None = every client. Text bend (9) and
    # freehand drawing (6/7) have no chat op, so no kind carries those numbers.
    min_protocol: int | None = None
    # Tag the wording agent sees in ``already_in_edit`` when this kind is already satisfied.
    edit_tag: str = ""


@dataclass(frozen=True)
class SuggestionKind:
    """One suggestion kind. Lives in ``suggestions/kinds/<kind>.py`` and is registered by the
    explicit list in ``kinds/__init__.py``.

    ``witness`` and ``surfaces`` are mandatory for every kind, stub or not (a guard test): a kind
    that cannot be proven or has no declared home never reaches a client. ``flag`` names the
    ``Settings`` attribute that gates the kind (None = always on); ``legacy`` is the KRI-756
    engine row for a kind the chip engine still drives.
    """

    kind: str
    states: frozenset[str]
    required_signals: tuple[str, ...]
    availability: Callable[[SuggestionContext], bool]
    already_present: Callable[[SuggestionContext], bool]
    build_action: BuildAction
    witness: Witness
    surfaces: frozenset[str]
    flag: str | None = None
    min_protocol: int | None = None
    legacy: ChipKindSpec | None = None


# ── The v2 suggestion and set ────────────────────────────────────────────────────────


class SuggestionOut(BaseModel):
    """Wire projection of a ``Suggestion``: no action, no witness (those stay server-side)."""

    id: str = ""
    kind: str = ""
    title: str = ""
    why: str = ""
    evidence: list[str] = Field(default_factory=list)
    # The creator's chat message when they tap (e.g. "Mute clip 3, it has wind noise").
    prompt: str = ""
    # Hook / title cards only: the exact on-screen text the tap approves.
    copy_text: str | None = None
    rank: int = 1
    anchor: ChipAnchorOut = Field(default_factory=ChipAnchorOut)
    # editor_ops | brief_update | item_setting | copy_decision | async_generate
    action_type: str = ""
    # Generated media only: the cost shown before the creator consents.
    cost_usd: float | None = None
    requires_protocol: int | None = None


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    why: str = Field(default="", max_length=MAX_WHY_CHARS)
    evidence: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    copy_text: str | None = Field(default=None, max_length=MAX_COPY_CHARS)
    rank: int = Field(ge=1, le=MAX_SUGGESTIONS)
    anchor: ChipAnchorOut = Field(default_factory=ChipAnchorOut)
    requires_protocol: int | None = None
    action: Action
    witness_tier: str = "none"

    @field_validator("evidence")
    @classmethod
    def _bounded(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > MAX_EVIDENCE_CHARS for item in value):
            raise ValueError(f"evidence lines are 1..{MAX_EVIDENCE_CHARS} chars")
        return value

    def to_wire(self) -> SuggestionOut:
        return SuggestionOut(
            id=self.id,
            kind=self.kind,
            title=self.title,
            why=self.why,
            evidence=list(self.evidence),
            prompt=self.prompt,
            copy_text=self.copy_text,
            rank=self.rank,
            anchor=self.anchor,
            action_type=self.action.type,
            cost_usd=self.action.cost_usd if isinstance(self.action, AsyncGenerateAction) else None,
            requires_protocol=self.requires_protocol,
        )


class SuggestionSet(BaseModel):
    """The server-side set (what the offered-set ring stores); ``wire()`` is what goes out."""

    model_config = ConfigDict(extra="forbid")

    set_id: str = Field(min_length=1, max_length=64)
    digest: SuggestionDigest
    state: Literal["first_prompt", "ready"]
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=MAX_SUGGESTIONS)

    @model_validator(mode="after")
    def _ranked(self) -> SuggestionSet:
        ranks = [s.rank for s in self.suggestions]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("ranks must be 1..n in order")
        if len({s.id for s in self.suggestions}) != len(self.suggestions):
            raise ValueError("suggestion ids must be unique")
        return self

    def wire(self) -> list[SuggestionOut]:
        return [s.to_wire() for s in self.suggestions]


def suggestion_id(kind: str, target_keys: Sequence[str], base_digest: str) -> str:
    """Stable across serves of the same input, so a tap on a refreshed set still resolves."""
    raw = "|".join([kind, ",".join(sorted(target_keys)), base_digest])
    return f"ss_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


# ── What the agent returns ───────────────────────────────────────────────────────────


class InvalidAgentOutput(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RankedPick(BaseModel):
    """One pick. ``copy`` is only for kinds that carry creator-visible text (a hook or title);
    the server validates it before it can be shown. There is no field for an op."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    target_ids: list[str] = Field(max_length=MAX_TARGETS)
    why: str = Field(max_length=MAX_WHY_CHARS)
    copy_text: str | None = Field(default=None, alias="copy", max_length=MAX_COPY_CHARS)
    rank: int = Field(ge=1, le=MAX_SUGGESTIONS)


class SuggestionAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ranked: list[RankedPick] = Field(max_length=MAX_SUGGESTIONS)

    @model_validator(mode="after")
    def _distinct(self) -> SuggestionAgentOutput:
        if len({p.rank for p in self.ranked}) != len(self.ranked):
            raise ValueError("ranks must be unique")
        keys = [(p.kind, tuple(sorted(p.target_ids))) for p in self.ranked]
        if len(set(keys)) != len(keys):
            raise ValueError("a (kind, targets) pair may be picked once")
        return self


def agent_response_schema(kinds: Sequence[str], target_ids: Sequence[str]) -> dict[str, Any]:
    """The strict JSON schema sent with the call, closed over the kinds and target handles the
    server issued. Every key is required and ``copy`` is nullable (OpenAI strict mode)."""
    if not kinds:
        raise ValueError("a suggestion run needs at least one issued kind")
    handles = [WHOLE_VIDEO_HANDLE, *[t for t in target_ids if t != WHOLE_VIDEO_HANDLE]]
    pick = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "target_ids", "why", "copy", "rank"],
        "properties": {
            "kind": {"type": "string", "enum": list(kinds)},
            "target_ids": {"type": "array", "items": {"type": "string", "enum": handles}},
            "why": {"type": "string"},
            "copy": {"type": ["string", "null"]},
            "rank": {"type": "integer", "enum": list(range(1, MAX_SUGGESTIONS + 1))},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ranked"],
        "properties": {"ranked": {"type": "array", "items": pick}},
    }


def parse_agent_output(
    raw: str | Mapping[str, Any], *, kinds: Sequence[str], target_ids: Sequence[str]
) -> SuggestionAgentOutput:
    """Validate a model answer against what was issued. Anything outside it voids the answer:
    the caller falls back to deterministic ranking, it never repairs."""
    from pydantic import ValidationError  # noqa: PLC0415

    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        out = SuggestionAgentOutput.model_validate(data)
    except (ValueError, TypeError, ValidationError):
        raise InvalidAgentOutput("schema") from None
    issued_kinds = set(kinds)
    issued_targets = {WHOLE_VIDEO_HANDLE, *target_ids}
    for pick in out.ranked:
        if pick.kind not in issued_kinds:
            raise InvalidAgentOutput("unknown_kind")
        if not set(pick.target_ids) <= issued_targets:
            raise InvalidAgentOutput("unknown_target")
    return out
