"""Why a chat turn finished without delivering what was asked (KRI-1382).

A refusal ("I can't make this yet", "I couldn't turn that into a plan") completes the turn as an
ordinary assistant message, so the turn row carried no failure code and only `_fail_turn` turns
were countable. The code is recorded on the turn's own plan (`plan_json["diagnostics"]["outcome"]`)
rather than on `turn.error`: a completed turn with an `error` is read as broken elsewhere
(`media_collection_stories`), and a refusal is a complete answer. No migration.

Kinds:
- `refused`: the system said it will not or cannot do this as asked (policy, capability, a tap
  the commit could not honour).
- `blocked`: a draft was held back until the creator answers or fixes something.
- `recovery`: the system could not finish planning, or its work is still not ready.
- `superseded`: the creator sent a newer message while this turn was still working, so this
  turn's own answer was cut short. Not an attempt (see `creation_health`).
- `inert`: the message was not a creation request (a repeated send, a "what's happening" or "what
  can you do" question), answered without planning anything. Not an attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.kria.contracts import KriaTurnPlan
from app.services.choice_questions import KEEP_OPEN_REASON

OutcomeKind = Literal["refused", "blocked", "recovery", "superseded", "inert"]

OUTCOME_KEY = "outcome"
UNCLASSIFIED_RECOVERY = "unclassified_recovery"
_CODE_MAX_CHARS = 80

# `brief_coverage["stage"]` of a reply that ended the turn without a draft, and what it means.
_COVERAGE_STAGE_KIND: dict[str, OutcomeKind] = {
    "draft": "blocked",
    "planning": "recovery",
    "analysis": "recovery",
}
_ANALYSIS_PENDING = "clip_analysis_pending"
# A draft held back by a question reads the same whichever gate asked (the commit gate's
# brief_coverage reason is `KEEP_OPEN_REASON`).
UNRESOLVED_CHOICE = KEEP_OPEN_REASON
MOMENT_ORDER_QUESTION = "moment_order_question"


def stamp_outcome(plan: KriaTurnPlan, kind: OutcomeKind, code: str) -> KriaTurnPlan:
    """`plan` with its outcome recorded; an outcome already on it is never replaced."""
    if read_outcome(plan.diagnostics) is not None:
        return plan
    return plan.model_copy(
        update={"diagnostics": {**(plan.diagnostics or {}), **outcome_diagnostics(kind, code)}}
    )


def outcome_diagnostics(kind: OutcomeKind, code: str) -> dict[str, Any]:
    """`KriaTurnPlan.diagnostics` for a plan that ends with this outcome."""
    return {OUTCOME_KEY: {"kind": kind, "code": code[:_CODE_MAX_CHARS]}}


def refusal_diagnostics(code: str) -> dict[str, Any]:
    return outcome_diagnostics("refused", code)


def reason_code_slug(reason_code: str) -> str:
    """A model-written free `reason_code` as a stable, countable code."""
    spaced = "".join(ch if ch.isascii() and ch.isalnum() else " " for ch in reason_code.lower())
    return "_".join(spaced.split()) or "unspecified"


def read_outcome(diagnostics: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """(kind, code) recorded on a plan's diagnostics, else None."""
    record = (diagnostics or {}).get(OUTCOME_KEY)
    if not isinstance(record, Mapping):
        return None
    kind, code = record.get("kind"), record.get("code")
    if isinstance(kind, str) and isinstance(code, str) and code:
        return kind, code
    return None


def classify_response_turn(
    plan: KriaTurnPlan,
    brief_coverage: Mapping[str, Any] | None,
    pinned_refusal: Mapping[str, Any] | None,
) -> KriaTurnPlan:
    """Stamp the outcome of a response-only turn from what the caller already knows.

    Every response-only turn ends in `_complete_response_turn`, so this is the one place that
    guarantees a coded outcome even for a reply path that did not stamp its own. An ordinary
    question or answer matches nothing here and stays unstamped (a success); a conflict question
    ("none of your clips has a filming time, so I can't order them") is a held-back draft.
    """
    if read_outcome(plan.diagnostics) is not None:
        return plan
    coverage = brief_coverage or {}
    stage = coverage.get("stage")
    reason = coverage.get("reason")
    kind = _COVERAGE_STAGE_KIND.get(stage) if isinstance(stage, str) else None
    if kind is not None and isinstance(reason, str) and reason:
        return stamp_outcome(plan, kind, reason)
    if stage == "analysis":
        return stamp_outcome(plan, "recovery", _ANALYSIS_PENDING)
    pinned_reason = (pinned_refusal or {}).get("reason")
    if isinstance(pinned_reason, str) and pinned_reason:
        return stamp_outcome(plan, "refused", f"pin_{pinned_reason}")
    choice = plan.choice_question or {}
    if isinstance(choice.get("kind"), str) and choice["kind"]:
        # A conflict question names its collector `kind`; a model's own question has none.
        return stamp_outcome(plan, "blocked", UNRESOLVED_CHOICE)
    if plan.moment_order_question:
        return stamp_outcome(plan, "blocked", MOMENT_ORDER_QUESTION)
    if plan.turn_value == "recovery":
        diagnostic_reason = (plan.diagnostics or {}).get("reason")
        code = diagnostic_reason if isinstance(diagnostic_reason, str) else UNCLASSIFIED_RECOVERY
        return stamp_outcome(plan, "recovery", code or UNCLASSIFIED_RECOVERY)
    return plan
