"""What an intent review says to the creator: receipt rows, one question, a settled repair.

Pure composition from a stored review (no database, no model). Every row and sentence comes from
the validated findings, the server catalog and the closed copy tables in `intent_review_copy`;
nothing the model wrote reaches the chat. A review that was not proven to have shown the model its
frames, or that cannot be read back exactly, raises `UncheckedReview` and says nothing: there is no
fallback sentence.

Receipts use the contract the app already renders: an `assistant_response` event whose
`requirement_receipts` rows are `RequirementReceipt` (status met / partial / not_possible, the
brief requirement's own id), with `reply_lead` and `also_changed` riding next to them. A question is
an `assistant_question` with fixed options. Unverifiable and withheld (audio) asks get no row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.agents._schemas.intent_review import IntentFinding, counts_as_met
from app.kria.contracts import RequirementReceipt
from app.kria.intent_review_catalog import ReviewCatalog
from app.kria.intent_review_copy import (
    INTENT_REVIEW_TURN_VALUE,
    UncheckedReview,
    fix_done,
    fix_optional,
    headline_checked,
    headline_reverted,
    headline_reverted_over_newer_edits,
    headline_staged,
    headline_suggestion,
    headline_unverified,
    headline_verified,
    question_options,
    question_text,
    reason_checked,
    reason_fix_staged,
    reason_fix_undone,
    reason_fix_unverified,
    reason_fix_verified,
    reason_may_not_match,
    require_visual_inspection,
)
from app.kria.intent_review_disposition import Disposition
from app.kria.reply_language import reply_language_for

SCHEMA_VERSION = 1
MAX_REASON_CHARS = 300
POSTABLE_STATUSES = frozenset(
    {
        "no_findings",
        "suggestion",
        "needs_confirmation",
        "repair_staged",
        "repair_verified",
        "repair_unverified",
        "repair_reverted",
    }
)
_SETTLED = frozenset({"repair_verified", "repair_unverified", "repair_reverted"})
# A held-back repair is still a sensible offer unless the creator's own work moved on.
_OFFER_STILL_SENSIBLE = frozenset(
    {"medium_confidence", "tier_review", "candidate_not_auto", "repair_cap"}
)


@dataclass(frozen=True)
class Post:
    kind: Literal["receipt", "question"]
    event_type: Literal["assistant_response", "assistant_question"]
    content: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Stamp:
    review_id: str
    brief_version: int | None
    generation_id: str | None

    def base(self) -> dict[str, Any]:
        return {
            "turn_value": INTENT_REVIEW_TURN_VALUE,
            "schema_version": SCHEMA_VERSION,
            "review_id": self.review_id,
            "render_generation_id": self.generation_id,
        }

    def row(self, brief_id: str, status: str, reason: str) -> dict[str, Any]:
        return RequirementReceipt(
            requirement_id=brief_id,
            brief_version=self.brief_version,
            generation_id=self.generation_id,
            status=status,
            verification="checked",
            stage="checked",
            reason=reason[:MAX_REASON_CHARS],
        ).model_dump(mode="json")


def _receipt(stamp: _Stamp, lead: str, rows: list[dict[str, Any]], also: list[str]) -> Post:
    payload = stamp.base()
    if rows:
        payload["requirement_receipts"] = rows
        payload["reply_lead"] = lead
        if also:
            payload["also_changed"] = also
    return Post("receipt", "assistant_response", lead, payload)


def _question(stamp: _Stamp, brief_id: str, literal: str | None) -> Post:
    options = question_options(literal)
    payload = {
        **stamp.base(),
        "requirement_id": brief_id,
        "options": options,
        "recommended_option": options[0],
    }
    return Post("question", "assistant_question", question_text(literal), payload)


def _may_not_match(catalog: ReviewCatalog, offers: list[tuple[str | None, str]]) -> str:
    reason = reason_may_not_match()
    for candidate_id, why in offers:
        candidate = catalog.candidate(candidate_id) if candidate_id else None
        if candidate is None or why not in _OFFER_STILL_SENSIBLE:
            continue
        phrase = fix_optional(candidate.kind, candidate.role, candidate.target)
        if phrase and len(reason) + 1 + len(phrase) <= MAX_REASON_CHARS and phrase not in reason:
            reason = f"{reason} {phrase}"
    return reason


def _brief_id(catalog: ReviewCatalog, alias: str) -> str:
    requirement = catalog.requirement(alias)
    if requirement is None:
        raise UncheckedReview(f"{alias} was not issued")
    return requirement.brief_id


def _literal(catalog: ReviewCatalog, alias: str) -> str | None:
    requirement = catalog.requirement(alias)
    return requirement.literal if requirement is not None else None


def _rows_in_catalog_order(
    stamp: _Stamp, catalog: ReviewCatalog, reasons: Mapping[str, tuple[str, str]]
) -> list[dict[str, Any]]:
    return [
        stamp.row(item.brief_id, *reasons[item.requirement_id])
        for item in catalog.requirements
        if item.requirement_id in reasons
    ]


def _original(status: str, stamp: _Stamp, review: Mapping[str, Any]) -> list[Post]:
    require_visual_inspection(review)
    catalog = ReviewCatalog.model_validate(review["catalog"])
    findings = [IntentFinding.model_validate(f) for f in review["findings"]]
    disposition = Disposition.model_validate(review["disposition"])
    kinds = {item.requirement_id: item.kind for item in catalog.requirements}
    reasons: dict[str, tuple[str, str]] = {
        f.requirement_id: ("met", reason_checked())
        for f in findings
        if counts_as_met(f.verdict, f.confidence, kinds.get(f.requirement_id))
    }
    if status == "no_findings":
        if not reasons:
            return []
        if disposition.ignored:
            for alias in disposition.ignored:
                reasons[alias] = ("partial", reason_may_not_match())
            rows = _rows_in_catalog_order(stamp, catalog, reasons)
            return [_receipt(stamp, headline_suggestion(), rows, [])]
        everything = len(reasons) == len(catalog.requirements) and not catalog.withheld
        rows = _rows_in_catalog_order(stamp, catalog, reasons)
        return [_receipt(stamp, headline_checked(everything_judged=everything), rows, [])]

    offers: dict[str, list[tuple[str | None, str]]] = {}
    for suggestion in disposition.suggestions:
        offers.setdefault(suggestion.requirement_id, []).append(
            (suggestion.candidate_id, suggestion.why)
        )
    asked = disposition.question.requirement_id if disposition.question else None
    for alias, held in offers.items():
        reasons[alias] = ("partial", _may_not_match(catalog, held))
    if asked is not None and status == "needs_confirmation":
        reasons[asked] = ("partial", reason_may_not_match())
    if status == "repair_staged":
        for alias in _repaired_aliases(review, catalog, disposition):
            reasons[alias] = ("partial", reason_fix_staged())
        lead, question = headline_staged(), None
    else:
        lead = headline_suggestion()
        question = asked if status == "needs_confirmation" else None
    if not any(kind == "partial" for kind, _ in reasons.values()):
        return []
    posts = [_receipt(stamp, lead, _rows_in_catalog_order(stamp, catalog, reasons), [])]
    if question is not None:
        posts.append(_question(stamp, _brief_id(catalog, question), _literal(catalog, question)))
    return posts


def _repaired_aliases(
    review: Mapping[str, Any], catalog: ReviewCatalog, disposition: Disposition
) -> list[str]:
    targeted = list((review.get("repair") or {}).get("targeted_brief_ids") or [])
    if targeted:
        return [item.requirement_id for item in catalog.requirements if item.brief_id in targeted]
    aliases: list[str] = []
    for candidate_id in disposition.repair_candidate_ids:
        candidate = catalog.candidate(candidate_id)
        for alias in candidate.requirement_ids if candidate else ():
            if alias not in aliases:
                aliases.append(alias)
    return aliases


def _fix_lines(review: Mapping[str, Any], catalog: ReviewCatalog) -> list[str]:
    lines: list[str] = []
    for candidate_id in (review.get("repair") or {}).get("candidate_ids") or []:
        candidate = catalog.candidate(candidate_id)
        line = fix_done(candidate.kind, candidate.role) if candidate else None
        if line and line not in lines:
            lines.append(line)
    return lines


def _settled(
    status: str,
    reason: str | None,
    stamp: _Stamp,
    review: Mapping[str, Any],
    child_review: Mapping[str, Any] | None,
) -> list[Post]:
    catalog = ReviewCatalog.model_validate(review["catalog"])
    targets = [str(t) for t in (review.get("repair") or {}).get("targeted_brief_ids") or []]

    def rows(outcome: str, why: str) -> list[dict[str, Any]]:
        return [stamp.row(brief_id, outcome, why) for brief_id in targets]

    if status == "repair_unverified":
        return [
            _receipt(
                stamp,
                headline_unverified(),
                rows("partial", reason_fix_unverified()),
                _fix_lines(review, catalog),
            )
        ]
    require_visual_inspection(child_review)
    if status == "repair_verified":
        return [
            _receipt(
                stamp,
                headline_verified(),
                rows("met", reason_fix_verified()),
                _fix_lines(review, catalog),
            )
        ]
    if reason == "worse":
        return [_receipt(stamp, headline_reverted(), rows("partial", reason_fix_undone()), [])]
    if reason == "worse_head_moved":
        return [_receipt(stamp, headline_reverted_over_newer_edits(), [], [])]
    raise UncheckedReview(f"no wording for a repair reverted as {reason}")


def compose_posts(
    *,
    review_id: str,
    status: str,
    reason: str | None,
    review: Mapping[str, Any],
    brief_version: int | None,
    generation_id: str | None,
    child_review: Mapping[str, Any] | None = None,
    language: str | None = None,
) -> list[Post]:
    """The events an outcome adds to the chat, in the order they are written; [] for silence."""
    if status not in POSTABLE_STATUSES:
        return []
    stamp = _Stamp(review_id, brief_version, generation_id)
    try:
        with reply_language_for(language):
            if status in _SETTLED:
                return _settled(status, reason, stamp, review, child_review)
            return _original(status, stamp, review)
    except UncheckedReview:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UncheckedReview("the stored review cannot be read back") from exc
