"""Deterministic per-requirement checks and receipt-driven replies (KRI-188).

No model is involved. Each checker compares one Creative Brief requirement with
facts read from the drafted plan (a strategy or an editor payload) and returns a
``RequirementReceipt``. A requirement with no checker is reported ``partial``
("could not be verified"): the reply never claims what the server did not check.

Checks implemented: per-clip text coverage, ordering vs the requested key,
duration within +/-10%, and literal on-screen text.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt

DURATION_TOLERANCE = 0.10
MAX_REPLY_CHARS = 1200
_CAPTURE_ORDER_KEYS = {"capture_time", "chronological", "route", "time", "shot_order"}
_CAPTURE_BASES = {"capture_time", "route", "capture_order"}


def _fold(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


@dataclass(frozen=True)
class PlanFacts:
    """What a drafted plan verifiably contains. Missing facts stay None/empty."""

    clip_ids: tuple[str, ...] = ()
    title: str | None = None
    per_clip_text: dict[str, str] = field(default_factory=dict)
    inferred_text: dict[str, str] = field(default_factory=dict)
    positional_labels: tuple[str, ...] = ()
    duration_s: float | None = None
    ordering_basis: str | None = None
    ordering_fallback_clip_ids: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()


def plan_facts_from_strategy(
    strategy: Mapping[str, Any] | None, *, clip_ids: Iterable[str] = ()
) -> PlanFacts:
    """Read verifiable facts off a serialized ``CreativeStrategy``."""
    strategy = strategy or {}
    per_clip: dict[str, str] = {}
    inferred: dict[str, str] = {}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "label":
            continue
        for assignment in intent.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            media_id = str(assignment.get("media_id") or "")
            value = assignment.get("value")
            if not media_id or not value:
                continue
            per_clip[media_id] = str(value)
            # Only the creator's own words are "read"; everything the server
            # matched from footage or the record is reported as inferred.
            if assignment.get("grounding") != "creator_text":
                inferred[media_id] = str(value)
    labels = tuple(str(x) for x in (strategy.get("shot_labels") or []) if str(x).strip())
    title = strategy.get("opening_title")
    texts = [
        str(value)
        for value in (
            title,
            strategy.get("closing_title"),
            *labels,
            *per_clip.values(),
        )
        if value
    ]
    duration = strategy.get("target_duration_s")
    basis = strategy.get("ordering_basis")
    if basis is None and strategy.get("archetype") == "day_vlog":
        basis = "capture_order"
    return PlanFacts(
        clip_ids=tuple(str(c) for c in clip_ids),
        title=str(title) if title else None,
        per_clip_text=per_clip,
        inferred_text=inferred,
        positional_labels=labels,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in (strategy.get("ordering_fallback_clip_ids") or [])
        ),
        texts=tuple(texts),
    )


def plan_facts_from_editor_payload(payload: Mapping[str, Any] | None) -> PlanFacts:
    """Editor drafts expose only literal on-screen text to the checkers."""
    if not payload:
        return PlanFacts()
    strings: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(json.dumps(payload, default=str)))
    return PlanFacts(texts=tuple(strings))


def _receipt(
    req: BriefRequirement, status: str, reason: str | None, inferred: Iterable[str] = ()
) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=req.id,
        status=status,  # type: ignore[arg-type]
        reason=reason[:300] if reason else None,
        inferred=list(dict.fromkeys(str(x) for x in inferred))[:24],
    )


def _check_per_clip_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    total = len(facts.clip_ids)
    if facts.clip_ids:
        covered = [clip for clip in facts.clip_ids if clip in facts.per_clip_text]
        count = len(covered)
    else:
        count = max(len(facts.per_clip_text), len(facts.positional_labels))
    inferred = [
        f"{facts.inferred_text[clip]}" for clip in facts.clip_ids if clip in facts.inferred_text
    ] or list(facts.inferred_text.values())
    if total and count >= total:
        return _receipt(req, "met", None, inferred)
    if count == 0:
        return _receipt(
            req,
            "not_possible",
            "No clip got its own text in this draft."
            if not total
            else f"None of the {total} clips got its own text in this draft.",
        )
    reason = f"Text landed on {count} of {total} clips." if total else f"Text on {count} clips."
    return _receipt(req, "partial", reason, inferred)


def _check_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    key = str(req.facts.get("key") or req.facts.get("by") or "").casefold()
    if not facts.ordering_basis:
        return _receipt(req, "partial", "I can't confirm the order this draft uses.")
    if key in _CAPTURE_ORDER_KEYS and facts.ordering_basis not in _CAPTURE_BASES:
        return _receipt(
            req,
            "partial",
            f"This draft is ordered by {facts.ordering_basis.replace('_', ' ')}, "
            "not the order you asked for.",
        )
    if facts.ordering_fallback_clip_ids:
        n = len(facts.ordering_fallback_clip_ids)
        return _receipt(
            req,
            "partial",
            f"{n} clip{'s' if n != 1 else ''} had no capture time, so I kept "
            f"{'their' if n != 1 else 'its'} attachment order.",
        )
    return _receipt(req, "met", None)


def _check_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    target = req.facts.get("duration_s")
    if not isinstance(target, (int, float)) or target <= 0:
        return _receipt(req, "partial", "I can't verify this timing automatically.")
    if facts.duration_s is None:
        return _receipt(req, "partial", "I can't confirm this draft's length yet.")
    if abs(facts.duration_s - float(target)) <= DURATION_TOLERANCE * float(target):
        return _receipt(req, "met", None)
    return _receipt(
        req,
        "partial",
        f"This draft is about {facts.duration_s:g}s; you asked for {float(target):g}s.",
    )


def _check_literal_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    wanted = _fold(req.literal or "")
    if req.scope == "title":
        found = bool(facts.title) and _fold(facts.title or "") == wanted
        if not found and facts.title is None:
            found = any(wanted and wanted in _fold(t) for t in facts.texts)
    else:
        found = any(wanted and wanted in _fold(t) for t in facts.texts)
    if found:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "That exact text isn't in this draft.")


def check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if req.kind == "text":
        if req.scope == "per_clip" or req.scope.startswith("clip:"):
            return _check_per_clip_text(req, facts)
        if req.literal:
            return _check_literal_text(req, facts)
    elif req.kind == "order":
        return _check_order(req, facts)
    elif req.kind == "timing":
        return _check_timing(req, facts)
    return _receipt(req, "partial", "I can't verify this one automatically yet.")


def build_receipts(
    requirements: Iterable[BriefRequirement], facts: PlanFacts
) -> list[RequirementReceipt]:
    return [check_requirement(req, facts) for req in requirements if req.live]


# ------------------------------------------------------------------------ reply

_LABEL = {"met": "Done", "partial": "Partly", "not_possible": "Couldn't"}


def reply_from_receipts(
    brief: CreativeBrief,
    receipts: list[RequirementReceipt],
    *,
    summary: str | None = None,
) -> str:
    """Compose the creator-facing reply from receipts only.

    The model's free-text summary is kept only when every requirement is met;
    otherwise the reply is exactly what was checked, so it cannot overclaim.
    """
    by_id = {req.id: req for req in brief.requirements}
    lines: list[str] = []
    guesses: list[str] = []
    for receipt in receipts:
        req = by_id.get(receipt.requirement_id)
        what = req.text() if req else receipt.requirement_id
        line = f"{_LABEL[receipt.status]}: {what}"
        if receipt.reason and receipt.status != "met":
            line += f" ({receipt.reason.rstrip('.')})"
        lines.append(line)
        guesses.extend(receipt.inferred)
    if guesses:
        shown = ", ".join(dict.fromkeys(guesses))
        lines.append(f"I guessed these, tell me if any is wrong: {shown}")
    all_met = bool(receipts) and all(r.status == "met" for r in receipts)
    head = ""
    if all_met and summary and summary.strip():
        head = summary.strip() + "\n"
    elif not all_met:
        head = "Not everything you asked for made it in:\n"
    text = head + "\n".join(f"- {line}" for line in lines)
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


__all__ = [
    "PlanFacts",
    "build_receipts",
    "check_requirement",
    "plan_facts_from_editor_payload",
    "plan_facts_from_strategy",
    "reply_from_receipts",
]
