"""Deterministic judgment of a model's intent review: validate it, then decide what it means.

The model only says which issued requirement looks unmet, why (a closed defect kind), on which
frames and how sure it is. Everything that could change the creator's video is decided here from
the server catalog: a finding becomes a repair only through a catalog candidate whose own
precondition held, at high confidence, on a tier that repairs, with the thread quiet. Any
violation of the output contract voids the whole review, so a false "everything is there" can
never be recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents._schemas.intent_review import (
    DEFECTS_BY_REQUIREMENT_KIND,
    IntentFinding,
    IntentReviewOutput,
    InvalidReviewOutput,
    parse_review_output,
)
from app.kria.intent_review_catalog import ReviewCatalog
from app.kria.intent_review_tier import tier_rank

MAX_REPAIR_CANDIDATES = 3
_TEXT_KINDS = frozenset({"text", "text_sequence"})
_WORDING_DEFECTS = frozenset({"copy_mismatch", "text_missing"})

Outcome = Literal["no_findings", "suggestion", "needs_confirmation", "repair"]
Why = Literal[
    "no_candidate",
    "render_fidelity",
    "candidate_not_auto",
    "medium_confidence",
    "tier_review",
    "head_moved",
    "thread_busy",
    "protected",
    "repair_cap",
]


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    candidate_id: str | None = None
    why: Why


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str


class Disposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Outcome
    repair_candidate_ids: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    question: Question | None = None
    ignored: list[str] = Field(default_factory=list)


Blocked = Literal["head_moved", "thread_busy", "protected"]


@dataclass(frozen=True)
class RepairPreconditions:
    """What the staging step found under lock; `blocked` names the first thing in the way."""

    blocked: Blocked | None = None
    # Candidates the live brief still allows; None means the check was not made.
    allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class Evaluation:
    status: Literal[
        "no_findings", "suggestion", "needs_confirmation", "repair_staged", "unavailable"
    ]
    reason: str
    findings: list[IntentFinding]
    disposition: Disposition | None


def validate_review_output(
    output: IntentReviewOutput, catalog: ReviewCatalog
) -> list[IntentFinding]:
    issued = {item.requirement_id: item for item in catalog.requirements}
    frames = set(catalog.frame_ids)
    checked: list[IntentFinding] = []
    for finding in output.findings:
        requirement = issued.get(finding.requirement_id)
        if requirement is None:
            raise InvalidReviewOutput("unknown_requirement")
        if not set(finding.evidence_frame_ids) <= frames:
            raise InvalidReviewOutput("unknown_frame")
        if (
            finding.verdict == "unmet"
            and finding.defect_kind not in DEFECTS_BY_REQUIREMENT_KIND[requirement.kind]
        ):
            raise InvalidReviewOutput("incompatible_defect_kind")
        if finding.verdict == "met" and requirement.kind == "timing":
            # Stills sample instants; they cannot show how long anything stays on screen.
            finding = finding.model_copy(
                update={"verdict": "unverifiable", "evidence_frame_ids": []}
            )
        checked.append(finding)
    if {f.requirement_id for f in checked} != set(issued):
        # The strict schema cannot force coverage. A reply that goes quiet on a requirement
        # must not be stored as the same clean pass as one that checked everything.
        raise InvalidReviewOutput("missing_requirement")
    return checked


def _repair_block(finding, tier: str, preconditions: RepairPreconditions | None) -> Why | None:
    if finding.confidence != "high":
        return "medium_confidence"
    if tier_rank(tier) < tier_rank("repair") or preconditions is None:
        return "tier_review"
    return preconditions.blocked


def _candidate_block(candidate, blocked: Why | None, preconditions) -> Why | None:
    if blocked is not None:
        return blocked
    allowed = preconditions.allowed if preconditions is not None else None
    return None if allowed is None or candidate.candidate_id in allowed else "protected"


def dispose(
    findings: list[IntentFinding],
    catalog: ReviewCatalog,
    *,
    tier: str,
    repair_preconditions: RepairPreconditions | None,
) -> Disposition:
    order = {alias: index for index, alias in enumerate(catalog.requirement_ids)}
    repairs: list[str] = []
    suggestions: list[Suggestion] = []
    ignored: list[str] = []
    unconfirmed: list[str] = []
    for finding in sorted(
        (f for f in findings if f.verdict == "unmet"), key=lambda f: order[f.requirement_id]
    ):
        alias = finding.requirement_id
        if finding.confidence == "low":
            ignored.append(alias)
            continue
        requirement = catalog.requirement(alias)
        matching = [
            c
            for c in catalog.candidates
            if alias in c.requirement_ids and finding.defect_kind in c.defect_kinds
        ]
        automatic = [c for c in matching if c.auto]
        if automatic:
            blocked = _repair_block(finding, tier, repair_preconditions)
            for candidate in automatic:
                if candidate.candidate_id in repairs:
                    continue
                held = _candidate_block(candidate, blocked, repair_preconditions)
                if held is None and len(repairs) < MAX_REPAIR_CANDIDATES:
                    repairs.append(candidate.candidate_id)
                else:
                    suggestions.append(
                        Suggestion(
                            requirement_id=alias,
                            candidate_id=candidate.candidate_id,
                            why=held or "repair_cap",
                        )
                    )
        elif matching:
            suggestions.extend(
                Suggestion(
                    requirement_id=alias,
                    candidate_id=candidate.candidate_id,
                    why="candidate_not_auto",
                )
                for candidate in matching
            )
        elif requirement.kind in _TEXT_KINDS and requirement.draft_matches_authority:
            suggestions.append(Suggestion(requirement_id=alias, why="render_fidelity"))
        elif (
            requirement.kind in _TEXT_KINDS
            and finding.confidence == "high"
            and finding.defect_kind in _WORDING_DEFECTS
        ):
            unconfirmed.append(alias)
        else:
            suggestions.append(
                Suggestion(
                    requirement_id=alias,
                    why="medium_confidence"
                    if finding.confidence != "high" and requirement.kind in _TEXT_KINDS
                    else "no_candidate",
                )
            )
    question = None
    for alias in unconfirmed:
        if question is None and not repairs:
            question = Question(requirement_id=alias)
        else:
            suggestions.append(Suggestion(requirement_id=alias, why="no_candidate"))
    outcome: Outcome = (
        "repair"
        if repairs
        else "needs_confirmation"
        if question
        else "suggestion"
        if suggestions
        else "no_findings"
    )
    return Disposition(
        outcome=outcome,
        repair_candidate_ids=repairs,
        suggestions=suggestions,
        question=question,
        ignored=ignored,
    )


def evaluate_review(
    raw_text: str,
    catalog: ReviewCatalog,
    *,
    tier: str,
    repair_preconditions: RepairPreconditions | None,
) -> Evaluation:
    """Parse, validate and dispose. Raises `InvalidReviewOutput` when the review is void."""
    findings = validate_review_output(parse_review_output(raw_text), catalog)
    judged = any(
        f.verdict == "met" or (f.verdict == "unmet" and f.confidence != "low") for f in findings
    )
    if not judged:
        return Evaluation("unavailable", "nothing_verifiable", findings, None)
    disposition = dispose(findings, catalog, tier=tier, repair_preconditions=repair_preconditions)
    if disposition.outcome == "no_findings" and not any(f.verdict == "met" for f in findings):
        return Evaluation("unavailable", "nothing_verifiable", findings, disposition)
    status = "repair_staged" if disposition.outcome == "repair" else disposition.outcome
    return Evaluation(status, "reviewed", findings, disposition)
