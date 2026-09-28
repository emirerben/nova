"""Data model for the request-following eval (KRI-185 P6a).

The question this harness answers is not "is each agent's output well formed" (the rest of
`tests/evals/` does that) but "did the finished edit do what the creator asked?".

A fixture is one creator thread over one footage set:

* `footage` — clip understanding + facts as JSON. Never video.
* `turns`  — what the creator said, and what the system did about it.
* `requirements` — the individually checkable things the creator asked for, each bound to a
  deterministic checker id.

Everything here is plain pydantic so a fixture is reviewable JSON and a scorer is a pure
function of (requirements, plan, reply).
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

RequirementKind = Literal["text", "order", "select", "timing", "audio", "style"]

# One entry per row of the KRI-185 coverage table; the KPI is reported per request type.
RequestType = Literal[
    "title_exact",
    "title_described",
    "label_exact",
    "label_described",
    "creator_facts",
    "order_chronological",
    "order_explicit",
    "order_route",
    "selection",
    "duration",
    "pacing",
    "readability",
    "restructure",
    "style",
]
REQUEST_TYPES: tuple[str, ...] = get_args(RequestType)

ScoreStatus = Literal["met", "partial", "unmet"]
TurnEngine = Literal["recorded", "v1_copilot", "v2_kria"]
Provenance = Literal["prod_capture", "authored"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Footage ──────────────────────────────────────────────────────────────────


class ClipFactRecord(_Strict):
    """Mirror of the P3 `ClipFact` (kind/value/provenance/confidence), kept local so the
    harness does not import a schema that lands in a sibling lane."""

    kind: Literal["capture_time", "place", "landmark", "visible_text", "creator"]
    value: str
    provenance: Literal["exif", "geocode", "vision", "creator", "inferred", "annotation"]
    confidence: float | None = None


class ClipRecord(_Strict):
    clip_id: str
    duration_s: float = Field(gt=0)
    subject: str = ""
    facts: list[ClipFactRecord] = Field(default_factory=list)
    # Hand/ground-truth annotations used only by checkers (never shown to a planner).
    route_rank: int | None = None
    true_landmark: str | None = None

    def fact(self, kind: str) -> ClipFactRecord | None:
        return next((f for f in self.facts if f.kind == kind), None)


class Footage(_Strict):
    footage_id: str
    description: str = ""
    clips: list[ClipRecord]

    @model_validator(mode="after")
    def _unique_ids(self) -> Footage:
        ids = [c.clip_id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate clip_id in footage")
        return self


# ── The finished edit, engine-agnostic ───────────────────────────────────────


class PlanClip(_Strict):
    clip_id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class PlanText(_Strict):
    id: str
    role: Literal["title", "label", "other"] = "other"
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    font_family: str | None = None
    # Set when the engine binds the text to one clip; otherwise time overlap decides.
    clip_id: str | None = None


class FinalPlan(_Strict):
    """What the creator would see: clips in output order + text lanes."""

    clips: list[PlanClip] = Field(default_factory=list)
    texts: list[PlanText] = Field(default_factory=list)
    total_duration_s: float | None = None

    @property
    def duration_s(self) -> float:
        if self.total_duration_s is not None:
            return self.total_duration_s
        return max((c.end_s for c in self.clips), default=0.0)

    def title(self) -> PlanText | None:
        return next((t for t in self.texts if t.role == "title"), None)


# ── Requirements ─────────────────────────────────────────────────────────────


class Requirement(_Strict):
    id: str
    kind: RequirementKind
    scope: str = "global"  # title | per_clip | clip:<id> | global
    request_type: RequestType
    checker: str
    params: dict[str, Any] = Field(default_factory=dict)
    # The creator's words this requirement was distilled from (for review, not scoring).
    source: str = ""
    # Turn index (0-based) at which the creator asked for it; it stays in force after.
    introduced_in_turn: int = 0
    # A reply that names one of these (regex, case-insensitive) claims the requirement was
    # handled. If the requirement is not met and the reply has no "not done" marker, that is
    # an overclaim — the KRI-185 trust bug.
    claim_terms: list[str] = Field(default_factory=list)


class RequirementScore(_Strict):
    requirement_id: str
    request_type: str
    status: ScoreStatus
    reason: str = ""
    reply_overclaims: bool = False

    @property
    def credit(self) -> float:
        return {"met": 1.0, "partial": 0.5, "unmet": 0.0}[self.status]


# ── Turns ────────────────────────────────────────────────────────────────────


class RecordedOutcome(_Strict):
    """A recorded (or hand-authored reference) result for one turn."""

    plan_after: FinalPlan
    reply: str | None = None
    # The edit this outcome restructured (only meaningful for `restructure` requirements).
    plan_before: FinalPlan | None = None


class Turn(_Strict):
    turn_id: str
    user_message: str
    engine: TurnEngine = "recorded"
    # engine == "recorded": the recorded outcome is authoritative.
    # engine == "v1_copilot": `copilot` holds the recorded agent input + raw model text; the
    #   outcome is computed by replaying it through the real copilot path.
    # engine == "v2_kria": `kria` holds a KriaReplayFixture dict for `replay_thread`.
    copilot: dict[str, Any] | None = None
    kria: dict[str, Any] | None = None
    # `recorded` is the authoritative outcome for engine == "recorded", and the recorded
    # plan for "v2_kria" (whose replay only inspects, it does not edit). Absent on a turn
    # that is authored but not yet recorded.
    recorded: RecordedOutcome | None = None
    # Requirement ids this turn's reply is responsible for (honesty audit). Defaults to the
    # requirements introduced in this turn.
    addresses: list[str] | None = None

    @model_validator(mode="after")
    def _engine_payload(self) -> Turn:
        if self.engine == "v1_copilot" and not self.copilot:
            raise ValueError("v1_copilot turn needs a `copilot` payload")
        if self.engine == "v2_kria" and not self.kria:
            raise ValueError("v2_kria turn needs a `kria` payload")
        return self


class RFFixture(_Strict):
    fixture_id: str
    provenance: Provenance
    footage: str  # footage_id under tests/fixtures/request_following/footage/
    description: str = ""
    turns: list[Turn]
    requirements: list[Requirement]
    # Known-good outcome for the FINAL turn. Proves every requirement is satisfiable by the
    # checkers (a checker nobody can pass is a broken checker). Required for authored
    # fixtures, optional for prod captures.
    reference: RecordedOutcome | None = None
    # Baseline statuses recorded when the fixture was captured/authored; the regression pin.
    baseline: dict[str, ScoreStatus] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> RFFixture:
        ids = [r.id for r in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate requirement id")
        turn_ids = [t.turn_id for t in self.turns]
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("duplicate turn_id")
        for req in self.requirements:
            if not 0 <= req.introduced_in_turn < len(self.turns):
                raise ValueError(f"{req.id}: introduced_in_turn out of range")
        for turn in self.turns:
            for rid in turn.addresses or []:
                if rid not in ids:
                    raise ValueError(f"{turn.turn_id}: addresses unknown requirement {rid}")
        unknown = set(self.baseline) - set(ids)
        if unknown:
            raise ValueError(f"baseline names unknown requirements: {sorted(unknown)}")
        if self.provenance == "authored" and self.reference is None:
            raise ValueError("authored fixtures must carry a reference outcome")
        return self


class TurnResult(_Strict):
    turn_id: str
    engine: TurnEngine
    plan_after: FinalPlan
    reply: str | None
    notes: list[str] = Field(default_factory=list)
    # True when the turn had no recording to replay (authored, awaiting recordings).
    unrecorded: bool = False


class ThreadResult(_Strict):
    fixture_id: str
    provenance: Provenance
    turns: list[TurnResult]
    scores: list[RequirementScore]
    # requirement_id -> did the reply of a turn that addressed it overclaim
    unrecorded: bool = False
