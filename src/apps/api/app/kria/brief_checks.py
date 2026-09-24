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
import re
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
    """NFC + Turkish-aware case fold ("KIRMIZI" == "Kırmızı"), whitespace collapsed."""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u0130", "i").replace("I", "i").replace("\u0131", "i")
    return " ".join(text.casefold().split())


def _contains_text(haystack: str, wanted: str) -> bool:
    """``wanted`` (already folded) appears in ``haystack`` as whole words.

    Substring matching would call "Go" met by "logo" or "Google Sans".
    """
    if not wanted:
        return False
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", _fold(haystack)) is not None


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
    # KRI-190: clips whose label is on a cut shorter than its reading time (the
    # clip itself is too short). A label the viewer cannot read is not "met".
    unreadable_label_clip_ids: tuple[str, ...] = ()
    # True when the facts come from an editor payload, which carries literal
    # on-screen text only (no per-clip structure, order or duration).
    editor: bool = False


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
    if (
        basis is None
        and strategy.get("archetype") == "day_vlog"
        and "ordering_fallback_clip_ids" in strategy
    ):
        # Capture order is only assumed when the planner also recorded which
        # clips fell back; otherwise nothing here can be verified.
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


def plan_facts_from_unified_montage(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a unified montage plan record (KRI-190).

    ``record`` is ``UnifiedMontagePlan.record()``: what the server actually put
    in the plan (the ordered clips, the labels it could ground, the title, the
    total length and how the order was decided), never what a model claimed.
    """
    record = record or {}
    labels = [row for row in record.get("labels") or [] if isinstance(row, Mapping)]
    per_clip = {str(row["media_id"]): str(row["text"]) for row in labels if row.get("text")}
    inferred = {
        str(row["media_id"]): str(row["text"])
        for row in labels
        if row.get("inferred") and row.get("text")
    }
    title = record.get("title")
    duration = record.get("duration_s")
    basis = record.get("ordering_basis")
    return PlanFacts(
        clip_ids=tuple(str(c) for c in record.get("clip_ids") or []),
        title=str(title) if title else None,
        per_clip_text=per_clip,
        inferred_text=inferred,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in record.get("ordering_fallback_clip_ids") or []
        ),
        texts=tuple(text for text in (title, *per_clip.values()) if text),
        unreadable_label_clip_ids=tuple(str(c) for c in record.get("short_label_clip_ids") or []),
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
    return PlanFacts(texts=tuple(strings), editor=True)


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
    wanted = _fold(req.literal or "")
    if facts.editor:
        # An editor payload has no per-clip structure: judge the literal if the
        # creator wrote one, otherwise say it can't be checked. Never "couldn't".
        if wanted and any(_contains_text(t, wanted) for t in facts.texts):
            return _receipt(req, "met", None)
        return _receipt(req, "partial", "I can't verify per-clip text on an editor edit.")
    ids = facts.clip_ids

    def text_for(index: int, clip: str) -> str | None:
        if clip in facts.per_clip_text:
            return facts.per_clip_text[clip]
        if index < len(facts.positional_labels):
            return facts.positional_labels[index]
        return None

    if req.scope.startswith("clip:"):
        clip = req.scope.split(":", 1)[1]
        index = ids.index(clip) if clip in ids else len(ids)
        value = text_for(index, clip)
        if value is None and clip not in ids and wanted:
            value = next((t for t in facts.texts if _contains_text(t, wanted)), None)
        if value is None:
            return _receipt(req, "not_possible", "That clip didn't get its own text in this draft.")
        if wanted and not _contains_text(value, wanted):
            return _receipt(req, "partial", "That clip's text isn't the exact text you gave.")
        guessed = [facts.inferred_text[clip]] if clip in facts.inferred_text else []
        return _receipt(req, "met", None, guessed)

    total = len(ids)
    if ids:
        count = sum(1 for i, clip in enumerate(ids) if text_for(i, clip) is not None)
    else:
        count = max(len(facts.per_clip_text), len(facts.positional_labels))
    inferred = [
        f"{facts.inferred_text[clip]}" for clip in ids if clip in facts.inferred_text
    ] or list(facts.inferred_text.values())
    if wanted and count:
        given = [*facts.per_clip_text.values(), *facts.positional_labels]
        if not any(_contains_text(t, wanted) for t in given):
            return _receipt(
                req, "partial", "The clips don't carry the exact text you gave.", inferred
            )
    if total and count >= total:
        if facts.unreadable_label_clip_ids:
            n = len(facts.unreadable_label_clip_ids)
            return _receipt(
                req,
                "partial",
                f"{n} clip{'s are' if n != 1 else ' is'} too short for its text to stay "
                "on screen long enough to read.",
                inferred,
            )
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
    basis = facts.ordering_basis
    if key in _CAPTURE_ORDER_KEYS:
        if basis not in _CAPTURE_BASES:
            return _receipt(
                req,
                "partial",
                f"This draft is ordered by {basis.replace('_', ' ')}, not the order you asked for.",
            )
    elif not key or key != basis:
        # Nothing here can confirm an ordering this checker has no rule for.
        return _receipt(req, "partial", "I can't verify this ordering automatically.")
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
    if req.scope == "title" and facts.title:
        found = _fold(facts.title) == wanted
    else:
        found = any(_contains_text(t, wanted) for t in facts.texts)
    if found:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "That exact text isn't in this draft.")


def _has_checker(req: BriefRequirement) -> bool:
    """True when ``check_requirement`` can actually verify this requirement."""
    if req.kind == "text":
        return bool(req.scope == "per_clip" or req.scope.startswith("clip:") or req.literal)
    if req.kind == "timing":
        # "Fast but readable" has no number to check: that is "can't verify"
        # (neutral in the reply), not a failed requirement.
        target = req.facts.get("duration_s")
        return isinstance(target, (int, float)) and not isinstance(target, bool) and target > 0
    return req.kind == "order"


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
    checkable = {
        r.requirement_id
        for r in receipts
        if (req := by_id.get(r.requirement_id)) is not None and _has_checker(req)
    }
    # "Can't verify" is neutral: only a requirement a checker actually judged
    # can turn the reply into a failure notice.
    problem = any(r.status != "met" and r.requirement_id in checkable for r in receipts)
    head = ""
    if not problem and summary and summary.strip():
        head = summary.strip() + "\n"
    elif problem:
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
    "plan_facts_from_unified_montage",
    "reply_from_receipts",
]
