"""Creative Brief: the thread-level, versioned requirement ledger (KRI-188).

The brief is what the creator asked for, stored as typed requirements instead of
chat text. Everything here is pure and deterministic except the two small
persistence helpers at the bottom; the model only ever *proposes* requirement
updates (``BriefUpdate``), and the server owns ids, supersession, routing and
status.

Scope router
------------
``route_requirements`` decides, without a model, whether a turn needs a fresh
plan (``replan`` -> ``draft.apply_strategy``) or can be served by reversible
editor operations (``editor_ops`` -> ``draft.apply_editor_ops``):

* ``replan`` when any new requirement is ``order`` or ``select``; when a
  per-clip text requirement arrives and the current plan has no per-clip text
  lane; when the request mixes requirement kinds; when there is no render to
  edit; or when the creator explicitly asks to redo it from their prompt.
* ``editor_ops`` otherwise (including turns with no new requirement).
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select

from app.kria.brief_route import wants_filming_time_text
from app.models import CreativeBriefVersion

RequirementKind = Literal["text", "order", "select", "timing", "audio", "style"]
RequirementStatus = Literal["open", "met", "partial", "not_possible", "superseded"]
Route = Literal["replan", "editor_ops"]

MAX_UPDATES_PER_TURN = 8
MAX_LIVE_REQUIREMENTS = 40
MAX_BRIEF_REQUEST_CHARS = 9_000
_SCOPE_RE = re.compile(r"^(title|per_clip|global|clip:[A-Za-z0-9._:-]{1,100})$")
_MAX_FACTS_BYTES = 2_000


def _nfc(value: str) -> str:
    """Turkish (and every other) text stays NFC; never folded to ASCII."""
    return unicodedata.normalize("NFC", value).strip()


class _BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefUpdate(_BriefModel):
    """One requirement as proposed by the planner model. Ids/status are server-owned."""

    kind: RequirementKind
    scope: str
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=300)
    facts: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> str:
        scope = _nfc(str(value or "")).replace(" ", "")
        if not _SCOPE_RE.match(scope):
            raise ValueError("scope must be title | per_clip | clip:<id> | global")
        return scope

    @field_validator("literal", "description", mode="before")
    @classmethod
    def _text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = _nfc(str(value))
        return text or None

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return {}
        return value if len(encoded.encode("utf-8")) <= _MAX_FACTS_BYTES else {}

    @model_validator(mode="after")
    def _needs_content(self) -> BriefUpdate:
        if not self.literal and not self.description:
            raise ValueError("a requirement needs a literal or a description")
        return self


class BriefRequirement(_BriefModel):
    id: str = Field(min_length=1, max_length=24)
    kind: RequirementKind
    scope: str
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=300)
    facts: dict[str, Any] = Field(default_factory=dict)
    source_turn_id: str | None = None
    status: RequirementStatus = "open"

    @property
    def live(self) -> bool:
        return self.status != "superseded"

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.scope)

    def text(self) -> str:
        """Creator-facing one-liner: the literal when exact, else the description."""
        if self.literal and self.description:
            return f'{self.description} ("{self.literal}")'
        if self.literal:
            return f'"{self.literal}"'
        return self.description or ""


class CreativeBrief(_BriefModel):
    version: int = Field(default=0, ge=0)
    requirements: list[BriefRequirement] = Field(default_factory=list)

    def live(self) -> list[BriefRequirement]:
        return [req for req in self.requirements if req.live]


def parse_brief_updates(raw: object) -> list[BriefUpdate]:
    """Tolerant parse of the model's ``brief_updates``: drop bad entries, never raise.

    Requirement extraction is best-effort; a malformed entry must not fail the
    whole planning turn (the plan itself is validated separately and strictly).
    """
    if not isinstance(raw, list):
        return []
    out: list[BriefUpdate] = []
    for item in raw[:MAX_UPDATES_PER_TURN]:
        try:
            out.append(BriefUpdate.model_validate(item))
        except (ValidationError, ValueError, TypeError):
            continue
    return out


def _next_id_number(requirements: Iterable[BriefRequirement]) -> int:
    highest = 0
    for req in requirements:
        match = re.fullmatch(r"r(\d+)", req.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def merge_requirements(
    old: Iterable[BriefRequirement],
    new: Iterable[BriefRequirement],
) -> list[BriefRequirement]:
    """Return the next ledger: a later requirement with the same (kind, scope)
    supersedes the earlier one.

    ``old`` items already superseded in a prior version are dropped (they live
    in the older version rows); items superseded *by this merge* are kept,
    flagged, so the transition is visible in the new version. Two entries in
    ``new`` with the same key collapse to the last one.
    """
    fresh: dict[tuple[str, str], BriefRequirement] = {}
    for req in new:
        fresh[req.key] = req.model_copy(update={"status": "open"})
    merged: list[BriefRequirement] = []
    for req in old:
        if not req.live:
            continue
        if req.key in fresh:
            merged.append(req.model_copy(update={"status": "superseded"}))
        else:
            merged.append(req)
    live_old = [req for req in merged if req.live]
    room = MAX_LIVE_REQUIREMENTS - len(live_old)
    merged.extend(list(fresh.values())[: max(room, 0)])
    return merged


def apply_updates(
    prior: CreativeBrief | None,
    updates: Iterable[BriefUpdate],
    *,
    source_turn_id: str | None,
) -> CreativeBrief:
    """Server-assign ids and merge ``updates`` into ``prior`` (version is set on persist)."""
    prior = prior or CreativeBrief()
    counter = _next_id_number(prior.requirements)
    incoming: list[BriefRequirement] = []
    for update in updates:
        incoming.append(
            BriefRequirement(
                id=f"r{counter}",
                kind=update.kind,
                scope=update.scope,
                literal=update.literal,
                description=update.description,
                facts=update.facts,
                source_turn_id=source_turn_id,
            )
        )
        counter += 1
    return CreativeBrief(
        version=prior.version, requirements=merge_requirements(prior.requirements, incoming)
    )


def new_requirements(before: CreativeBrief | None, after: CreativeBrief) -> list[BriefRequirement]:
    """Live requirements in ``after`` that are not already live in ``before``."""
    known = {req.id for req in (before.live() if before else [])}
    return [req for req in after.live() if req.id not in known]


# --------------------------------------------------------------------------- router


@dataclass(frozen=True)
class CurrentPlanShape:
    """What the router needs to know about the plan a render already carries."""

    has_render: bool
    # A per-clip label bar exists (a single label can be corrected in place).
    has_per_clip_text_lane: bool = False
    # The copilot can also FILL labels for every clip: a legacy lane, or the
    # server-only `label_each_clip` capability (clip facts on the snapshot).
    # None = same as `has_per_clip_text_lane` (callers that never distinguish).
    can_fill_per_clip_text: bool | None = None
    # KRI-219: the editor can reorder/remove clips on this variant (its snapshot
    # advertises the `clip` op family). None/False = today's behaviour: order and
    # select requirements always re-plan.
    can_edit_timeline: bool | None = None
    # KRI-219: the v2 `reorder_clips_by` op exists on this variant AND at least two
    # clips carry a capture-time fact, so "order by when I filmed them" is an
    # editor op, not a planner re-run.
    can_order_by_capture_time: bool = False


_PER_CLIP_LANE_ROLES = {"shot_label", "clip_label", "per_clip", "label"}

# Whole-edit redo phrases only. "make the title bigger again" / "cut the intro
# again" are ordinary edits and must stay on the editor-op path, so "again" only
# counts right after a whole-edit object or next to prompt/brief/request.
_REDO_PATTERNS = (
    re.compile(
        r"\b(do|make|create|generate|render|build|try|run)\s+"
        r"(it|this|that|the (video|edit)|everything|all of it)(\s+all)?\s+again\b"
    ),
    re.compile(r"\b(try|start)\s+again\b"),
    re.compile(r"\b(prompt|brief|request|instructions?)\b.{0,20}\bagain\b"),
    re.compile(r"\bagain\b.{0,20}\b(prompt|brief|request|instructions?)\b"),
    re.compile(r"\b(redo|re-do|start over|from scratch)\b"),
    re.compile(r"\bbased on (my|the) (prompt|brief|request|instructions?)\b"),
    re.compile(r"\b(ba\u015ftan|bastan)\b"),
    re.compile(r"\b(yeniden|tekrar)\s+(yap|olu\u015ftur|olustur|haz\u0131rla)\w*"),
)


_NEGATED_AGAIN = re.compile(r"\b(don'?t|dont|do not|never|stop)\b[^.!?]{0,20}\bagain\b")


def _fold_for_redo(message: str) -> str:
    # Turkish dotted/dotless I: casefold() turns "\u0130" into i + combining dot,
    # which no pattern would match, so map both capitals first.
    text = unicodedata.normalize("NFC", message or "").replace("\u0130", "i").replace("I", "\u0131")
    return " ".join(text.casefold().split())


def wants_full_replan(message: str) -> bool:
    """ "Do it again based on my prompt" is a supported re-plan, not an editor op."""
    text = _NEGATED_AGAIN.sub(" ", _fold_for_redo(message))
    return any(pattern.search(text) for pattern in _REDO_PATTERNS)


def plan_shape_from_editor_snapshot(snapshot: Mapping[str, Any] | None) -> CurrentPlanShape:
    """Derive the router's view of the current plan from the editor snapshot.

    Conservative on purpose: an unknown/absent per-clip lane reads as "no lane",
    which routes to the stronger re-plan path instead of a lossy editor op.
    """
    if not snapshot:
        return CurrentPlanShape(has_render=False)
    bars = snapshot.get("text_bars") or []
    legacy_lane = any(
        isinstance(bar, Mapping) and str(bar.get("role") or "") in _PER_CLIP_LANE_ROLES
        for bar in bars
    )
    # The unified montage writes its per-clip labels as `clip-label-*` bars with
    # role "generative_intro" (KRI-191), so the id prefix is the reliable lane
    # marker. Such a lane supports correcting ONE label (`edit_text`), but filling
    # every clip needs `label_each_clip`, which exists only when the snapshot
    # carries clip facts; without them the editor tool cannot serve "label every
    # clip" and the router must keep re-planning.
    label_bars = any(
        isinstance(bar, Mapping) and str(bar.get("id") or "").startswith("clip-label-")
        for bar in bars
    )
    can_edit_timeline = "clip" in (snapshot.get("allowed_op_families") or [])
    return CurrentPlanShape(
        has_render=True,
        has_per_clip_text_lane=legacy_lane or label_bars or _has_seen(snapshot),
        # `seen` (stored clip understanding) lets the copilot write per-clip captions with
        # add_text even where no place/time fact exists for label_each_clip (KRI-219).
        can_fill_per_clip_text=legacy_lane
        or snapshot.get("label_facts") is True
        or _has_seen(snapshot),
        can_edit_timeline=can_edit_timeline,
        can_order_by_capture_time=can_edit_timeline and _timed_slot_count(snapshot) >= 2,
    )


def _has_seen(snapshot: Mapping[str, Any]) -> bool:
    return any(
        isinstance(slot, Mapping)
        and not slot.get("removed")
        and isinstance(slot.get("seen"), Mapping)
        and slot["seen"].get("text")
        for slot in snapshot.get("slots") or []
    )


def _timed_slot_count(snapshot: Mapping[str, Any]) -> int:
    """Active slots carrying a capture-time fact (v2 snapshots only)."""
    if snapshot.get("editor_ops_version") != 2:
        return 0
    count = 0
    for slot in snapshot.get("slots") or []:
        if not isinstance(slot, Mapping) or slot.get("removed"):
            continue
        facts = slot.get("facts")
        if isinstance(facts, list) and any(
            isinstance(f, Mapping) and f.get("kind") == "capture_time" for f in facts
        ):
            count += 1
    return count


# Positional/explicit clip moves and removals an editor op can express. Semantic
# selection ("only the funniest clips") and basis-driven ordering ("by when I
# filmed them") are NOT matched: those need the planner.
_MOVE_MESSAGE = re.compile(
    r"\b(move|swap|put|place|shift|bring|send|reorder)\b.{0,80}"
    r"\b(start|beginning|begin|first|last|end|before|after|front|back|position|spot|second|third)\b"
)
_REMOVE_MESSAGE = re.compile(
    r"\b(remove|delete|drop|cut|get rid of|take out)\b.{0,20}"
    r"\b(clip|shot|video|segment|scene)\s*(#|no\.?\s*)?\d+\b"
    r"|\b(remove|delete|drop|cut|get rid of|take out)\b.{0,20}\b(the\s+)?"
    r"(first|last|second|third|fourth|fifth)\s+(clip|shot|video|segment|scene)\b"
)
_POSITIONAL_FACT_KEYS = {
    "index",
    "indices",
    "position",
    "positions",
    "clip_index",
    "clip_number",
    "clip_numbers",
}


def _structural_reqs_editable(
    reqs: list[BriefRequirement | BriefUpdate], message: str | None
) -> bool:
    """True when every order/select requirement is an explicit clip move/removal."""
    text = _fold_for_redo(message or "")
    for req in reqs:
        if req.kind == "order":
            if not req.facts.get("key") or _MOVE_MESSAGE.search(text):
                continue
            return False
        if req.kind == "select":
            if (
                req.scope.startswith("clip:")
                or _POSITIONAL_FACT_KEYS & set(req.facts)
                or _REMOVE_MESSAGE.search(text)
            ):
                continue
            return False
    return True


_CAPTURE_ORDER_KEYS = {"capture_time", "chronological", "time"}
_ORDER_ONLY_FACTS = {"key", "by", "direction", "order"}


def is_capture_order_requirement(req: BriefRequirement | BriefUpdate) -> bool:
    """An order requirement that is exactly "by when it was filmed" (no route)."""
    if req.kind != "order":
        return False
    facts = req.facts or {}
    key = str(facts.get("key") or facts.get("by") or "").casefold()
    return key in _CAPTURE_ORDER_KEYS and set(facts) <= _ORDER_ONLY_FACTS


def is_filming_time_label_requirement(req: BriefRequirement | BriefUpdate) -> bool:
    """A per-clip text requirement whose text is the hour each clip was filmed."""
    return wants_filming_time_text(req.kind, req.scope, req.literal, req.description, req.facts)


def _capture_time_ask_editable(
    reqs: list[BriefRequirement | BriefUpdate], current_plan: CurrentPlanShape
) -> bool:
    """True when EVERY requirement is an editor op over server-known capture times.

    "Order them by the time they were filmed and add the hour to each" is a
    `reorder_clips_by` + `label_each_clip` bundle, not a re-plan. Anything else in
    the ask (selection, place labels, audio, timing) keeps the planner.
    """
    if not current_plan.can_order_by_capture_time:
        return False
    can_fill = (
        current_plan.has_per_clip_text_lane
        if current_plan.can_fill_per_clip_text is None
        else current_plan.can_fill_per_clip_text
    )
    has_order = False
    for req in reqs:
        if is_capture_order_requirement(req):
            has_order = True
        elif is_filming_time_label_requirement(req):
            if not can_fill:
                return False
        elif req.kind == "text" and req.scope == "title" and req.literal:
            continue
        elif req.kind == "style":
            continue
        else:
            return False
    return has_order


def route_requirements(
    new_reqs: Iterable[BriefRequirement | BriefUpdate],
    current_plan: CurrentPlanShape | None,
    *,
    message: str | None = None,
) -> Route:
    """Deterministic scope router. See the module docstring for the rules."""
    reqs = list(new_reqs)
    if current_plan is None or not current_plan.has_render:
        return "replan"
    if message and wants_full_replan(message):
        return "replan"
    if not reqs:
        return "editor_ops"
    kinds = {req.kind for req in reqs}
    if _capture_time_ask_editable(reqs, current_plan):
        return "editor_ops"
    if kinds & {"order", "select"}:
        if not (
            current_plan.can_edit_timeline
            and kinds <= {"order", "select"}
            and _structural_reqs_editable(reqs, message)
        ):
            return "replan"
        return "editor_ops"
    if len(kinds) > 1 and not kinds <= {"text", "style"}:
        # Text and style are both in-place label/title tweaks the editor ops express
        # (KRI-219: "move the timestamps top left, make them smaller, just the hour");
        # any other mix (audio, timing, ...) still needs the planner.
        return "replan"
    per_clip_text = any(
        req.kind == "text" and (req.scope == "per_clip" or req.scope.startswith("clip:"))
        for req in reqs
    )
    if per_clip_text and not current_plan.has_per_clip_text_lane:
        return "replan"
    fills_every_clip = any(req.kind == "text" and req.scope == "per_clip" for req in reqs)
    can_fill = (
        current_plan.has_per_clip_text_lane
        if current_plan.can_fill_per_clip_text is None
        else current_plan.can_fill_per_clip_text
    )
    if fills_every_clip and not can_fill:
        return "replan"
    return "editor_ops"


# ---------------------------------------------------------------- request rendering


def render_brief_request(brief: CreativeBrief | None, *, latest_message: str = "") -> str:
    """Render the brief as the strategy's creator request.

    This replaces the chip-concatenated chat string: every live requirement is
    listed verbatim, so a requirement typed after a render can never be lost to
    truncation or chip boilerplate.
    """
    lines: list[str] = []
    if brief is not None:
        for req in brief.live():
            lines.append(f"- [{req.kind}/{req.scope}] {req.text()}")
    if not lines:
        return _nfc(latest_message)
    body = "Creative brief (everything the creator has asked for, still in force):\n" + "\n".join(
        lines
    )
    latest = _nfc(latest_message)
    if latest:
        body += f"\nLatest message: {latest}"
    return body[:MAX_BRIEF_REQUEST_CHARS]


def apply_receipt_statuses(
    brief: CreativeBrief, receipts: Iterable[Mapping[str, Any]]
) -> CreativeBrief:
    """Overlay the latest receipt outcome onto each live requirement (read-time view)."""
    by_id = {str(r.get("requirement_id")): str(r.get("status")) for r in receipts}
    updated = [
        req.model_copy(update={"status": by_id[req.id]})
        if req.live and by_id.get(req.id) in {"met", "partial", "not_possible"}
        else req
        for req in brief.requirements
    ]
    return CreativeBrief(version=brief.version, requirements=updated)


# --------------------------------------------------------------------- persistence


def _brief_from_row(row: CreativeBriefVersion | None) -> CreativeBrief | None:
    if row is None:
        return None
    reqs: list[BriefRequirement] = []
    for item in row.requirements or []:
        try:
            reqs.append(BriefRequirement.model_validate(item))
        except (ValidationError, ValueError):
            continue
    return CreativeBrief(version=int(row.version), requirements=reqs)


async def load_latest_brief(db: Any, thread_id: uuid.UUID) -> CreativeBrief | None:
    """Async read of the newest brief version (None when the thread has none)."""
    row = (
        await db.execute(
            select(CreativeBriefVersion)
            .where(CreativeBriefVersion.thread_id == thread_id)
            .order_by(CreativeBriefVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _brief_from_row(row)


def load_latest_brief_sync(db: Any, thread_id: uuid.UUID) -> CreativeBrief | None:
    row = db.execute(
        select(CreativeBriefVersion)
        .where(CreativeBriefVersion.thread_id == thread_id)
        .order_by(CreativeBriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _brief_from_row(row)


def persist_brief_version_sync(
    db: Any,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    updates: Iterable[BriefUpdate],
) -> CreativeBrief | None:
    """Append one version for ``turn_id``; idempotent per turn.

    Caller holds the thread row lock (the turn-completion transactions do), which
    serializes version numbering. Returns the effective brief, or None when there
    is neither a prior brief nor a new update (nothing to record).
    """
    updates = list(updates)
    existing = db.execute(
        select(CreativeBriefVersion).where(
            CreativeBriefVersion.thread_id == thread_id,
            CreativeBriefVersion.source_turn_id == turn_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _brief_from_row(existing)
    prior = load_latest_brief_sync(db, thread_id)
    if not updates:
        return prior
    merged = apply_updates(prior, updates, source_turn_id=str(turn_id))
    next_version = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreativeBriefVersion.version), 0)).where(
                    CreativeBriefVersion.thread_id == thread_id
                )
            ).scalar_one()
        )
        + 1
    )
    db.add(
        CreativeBriefVersion(
            thread_id=thread_id,
            version=next_version,
            requirements=[req.model_dump(mode="json") for req in merged.requirements],
            source_turn_id=turn_id,
        )
    )
    db.flush()
    return CreativeBrief(version=next_version, requirements=merged.requirements)


__all__ = [
    "BriefRequirement",
    "BriefUpdate",
    "CreativeBrief",
    "CurrentPlanShape",
    "apply_receipt_statuses",
    "apply_updates",
    "load_latest_brief",
    "load_latest_brief_sync",
    "merge_requirements",
    "new_requirements",
    "parse_brief_updates",
    "persist_brief_version_sync",
    "plan_shape_from_editor_snapshot",
    "render_brief_request",
    "route_requirements",
    "wants_full_replan",
]
