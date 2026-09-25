"""Scoring: per-requirement status, thread roll-up, and the KPI report.

KPI = share of requirements `met` (strict), reported per request type. `partial` is
reported separately and never rounds up: a half-followed brief is not a followed brief.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from .checkers import DISCLAIMER, EVENT_CHECKERS, fold, run_checker
from .models import (
    REQUEST_TYPES,
    FinalPlan,
    Footage,
    Requirement,
    RequirementScore,
    RFFixture,
    ThreadResult,
    TurnResult,
)


def reply_overclaims(req: Requirement, status: str, reply: str | None) -> bool:
    """True when the reply talks as if `req` was handled but it was not fully met, and
    nothing in the reply says so."""
    if reply is None or status == "met" or not req.claim_terms:
        return False
    text = unicodedata.normalize("NFC", reply)
    names_it = any(re.search(term, text, re.IGNORECASE) for term in req.claim_terms)
    return names_it and not DISCLAIMER.search(text)


def score(
    requirements: Sequence[Requirement],
    final_plan: FinalPlan,
    reply: str | None,
    *,
    footage: Footage,
    previous_plan: FinalPlan | None = None,
    addressed: set[str] | None = None,
) -> list[RequirementScore]:
    """met | partial | unmet for each requirement against the finished edit.

    `addressed` limits the reply-honesty audit to requirements this turn's reply is
    responsible for (None = all of them).
    """
    scores: list[RequirementScore] = []
    for req in requirements:
        status, reason = run_checker(req, final_plan, footage, previous_plan, reply)
        audited = addressed is None or req.id in addressed
        scores.append(
            RequirementScore(
                requirement_id=req.id,
                request_type=req.request_type,
                status=status,
                reason=reason,
                reply_overclaims=audited and reply_overclaims(req, status, reply),
            )
        )
    return scores


def score_thread(
    fixture: RFFixture, footage: Footage, turns: Sequence[TurnResult]
) -> list[RequirementScore]:
    """Final status per requirement (against the last turn's edit) + reply honesty
    accumulated over every turn that addressed it."""
    if len(turns) != len(fixture.turns):
        raise ValueError("turn results do not line up with fixture turns")
    overclaimed: dict[str, bool] = defaultdict(bool)
    last: dict[str, RequirementScore] = {}
    previous: FinalPlan | None = None
    for index, (spec, result) in enumerate(zip(fixture.turns, turns, strict=True)):
        # A state requirement stays in force and is re-judged on every later edit. An event
        # requirement ("do it again") is judged once, on the turn the creator asked.
        in_force = [
            r
            for r in fixture.requirements
            if r.introduced_in_turn == index
            or (r.introduced_in_turn < index and r.checker not in EVENT_CHECKERS)
        ]
        addressed = (
            set(spec.addresses)
            if spec.addresses is not None
            else {r.id for r in fixture.requirements if r.introduced_in_turn == index}
        )
        for item in score(
            in_force,
            result.plan_after,
            result.reply,
            footage=footage,
            previous_plan=previous,
            addressed=addressed,
        ):
            last[item.requirement_id] = item
            overclaimed[item.requirement_id] |= item.reply_overclaims
        previous = result.plan_after
    return [
        last[r.id].model_copy(update={"reply_overclaims": overclaimed[r.id]})
        for r in fixture.requirements
        if r.id in last
    ]


# ── KPI ──────────────────────────────────────────────────────────────────────


class TypeKPI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = 0
    met: int = 0
    partial: int = 0
    unmet: int = 0
    overclaims: int = 0

    @property
    def met_share(self) -> float | None:
        return self.met / self.total if self.total else None


class KPIReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_type: dict[str, TypeKPI]
    overall: TypeKPI
    threads_scored: int
    threads_unrecorded: int

    @property
    def met_share(self) -> float | None:
        return self.overall.met_share


def compute_kpi(results: Iterable[ThreadResult]) -> KPIReport:
    by_type = {t: TypeKPI() for t in REQUEST_TYPES}
    overall = TypeKPI()
    scored = unrecorded = 0
    for result in results:
        if result.unrecorded:
            unrecorded += 1
            continue
        scored += 1
        for item in result.scores:
            for bucket in (by_type[item.request_type], overall):
                bucket.total += 1
                setattr(bucket, item.status, getattr(bucket, item.status) + 1)
                bucket.overclaims += int(item.reply_overclaims)
    return KPIReport(
        by_type={k: v for k, v in by_type.items() if v.total},
        overall=overall,
        threads_scored=scored,
        threads_unrecorded=unrecorded,
    )


# ── Wrong-landmark rate ──────────────────────────────────────────────────────


def wrong_landmark_rate(
    footage: Footage, guesses: Mapping[str, str] | None = None
) -> tuple[float | None, int]:
    """Share of *inferred* landmark guesses that name the wrong place.

    D4 accepts best-guess landmark names, so the guess quality has to be measured. The
    denominator is clips that carry both a guess and a ground-truth `true_landmark`. The guess
    is the clip's `inferred` landmark fact, or `guesses[clip_id]` when given (so a live
    `landmark_guess` run can be scored against the same footage without rewriting fixtures).
    A guess is right when either name contains the other after case/diacritic folding.
    Returns `(None, 0)` when no clip has both.
    """
    judged = wrong = 0
    for clip in footage.clips:
        if guesses is not None and clip.clip_id in guesses:
            guess_text: str | None = guesses[clip.clip_id]
        else:
            fact = next(
                (f for f in clip.facts if f.kind == "landmark" and f.provenance == "inferred"),
                None,
            )
            guess_text = fact.value if fact else None
        if not guess_text or not clip.true_landmark:
            continue
        judged += 1
        guessed, truth = fold(guess_text), fold(clip.true_landmark)
        if truth not in guessed and guessed not in truth:
            wrong += 1
    return (wrong / judged if judged else None), judged
