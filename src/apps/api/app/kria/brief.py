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
    has_per_clip_text_lane: bool = False


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
    # The unified montage writes its per-clip labels as `clip-label-*` bars with
    # role "generative_intro" (KRI-191), so the id prefix is the reliable lane
    # marker; without it a per-clip label correction was routed to a re-plan.
    lane = any(
        isinstance(bar, Mapping)
        and (
            str(bar.get("role") or "") in _PER_CLIP_LANE_ROLES
            or str(bar.get("id") or "").startswith("clip-label-")
        )
        for bar in bars
    )
    return CurrentPlanShape(has_render=True, has_per_clip_text_lane=lane)


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
    if kinds & {"order", "select"}:
        return "replan"
    if len(kinds) > 1:
        return "replan"
    per_clip_text = any(
        req.kind == "text" and (req.scope == "per_clip" or req.scope.startswith("clip:"))
        for req in reqs
    )
    if per_clip_text and not current_plan.has_per_clip_text_lane:
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
