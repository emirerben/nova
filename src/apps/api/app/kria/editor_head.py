"""The editor draft head always sits on the variant's current generation.

An AI chat edit is a ``CreatorEditDraft`` head staged on the generation the variant had
when the edit was made. An approval render, a phone publish or a legacy dispatcher then
moves the variant to a newer generation, and nothing re-based the head. This module is
the one definition of "current" and the one way to move a head onto it.

Lock order is Plan, Item, Job, Session, Turn, Draft. The caller already holds Item and
Job; the head is locked last, here. Never call ``record_pipeline_event`` from this module:
it opens its own connection to the jobs row and self-deadlocks under FOR UPDATE.

``drafts.py`` and the services import this module, so the draft helpers are imported
inside the restage function rather than at module level.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import CreatorEditDraft, Job, PlanItem

log = structlog.get_logger(__name__)

_MAX_RESTAGE_CHAIN = 16


def variant_baseline(variant: dict[str, Any]) -> str:
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    return variant_render_baseline(variant)


def current_bases(job: Any, variant: dict[str, Any]) -> frozenset[str]:  # noqa: ANN401
    """Generations a draft may name and still be on the variant's current document.

    The render baseline, plus the generation a phone publish was made from: publishing
    replaces ``render_generation_id`` with the upload attempt id while the Save that
    produced it pinned the generation the phone was asked to render.
    """
    from app.services.device_render import published_source_generation  # noqa: PLC0415

    return frozenset(
        base
        for base in (variant_baseline(variant), published_source_generation(job, variant))
        if base
    )


def _payload_base(head: CreatorEditDraft) -> str | None:
    """The generation stamped inside the head's editor payload; None when it has none."""
    snapshot = head.snapshot_json
    payload = snapshot.get("editor_payload") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("base_generation") or "")


def is_head_current(job: Any, variant: dict[str, Any], head: CreatorEditDraft) -> bool:  # noqa: ANN401
    """The head belongs to this job and both of its generation stamps are current."""
    if head.base_job_id != job.id:
        return False
    bases = current_bases(job, variant)
    if not variant_baseline(variant):
        # A variant that never finished a render has nothing to be stale against; its
        # drafts carry the empty stamp.
        bases = bases | {""}
    if (head.base_generation_id or "") not in bases:
        return False
    payload_base = _payload_base(head)
    return payload_base is None or payload_base in bases


def is_head_on_baseline(job: Any, variant: dict[str, Any], head: CreatorEditDraft) -> bool:  # noqa: ANN401
    """Stricter than ``is_head_current``: both stamps equal the baseline itself.

    A head current only through a phone publish still names the pre-publish generation;
    it is moved to the exact id the first time anyone observes it.
    """
    baseline = variant_baseline(variant)
    payload_base = _payload_base(head)
    return (
        head.base_job_id == job.id
        and (head.base_generation_id or "") == baseline
        and (payload_base is None or payload_base == baseline)
    )


# A restage is the head moved onto a newer generation by something other than the creator's
# edit. Only the approval's own render and a phone publish (same document, new generation id)
# leave an approval's draft "the one the creator is looking at"; a manual Save, a speech cut, a
# repair or a heal after a legacy dispatcher does not.
OWN_RENDER_REASONS = frozenset({"approval_execution", "phone_publish"})


def restage_marker(row: Any) -> dict[str, Any] | None:  # noqa: ANN401
    snapshot = getattr(row, "snapshot_json", None)
    marker = snapshot.get("restage") if isinstance(snapshot, dict) else None
    return marker if isinstance(marker, dict) else None


def is_restage_row(row: Any) -> bool:  # noqa: ANN401
    return restage_marker(row) is not None


def draft_still_current(db: Session, draft: CreatorEditDraft) -> bool:
    """The creator has not moved past ``draft``: it is the head, or the head is its own render.

    Every link from the head back to ``draft`` must be a restage (it records ``restage.of`` and
    a reason) whose reason is the approval's own render or a phone publish, and no link may have
    replaced a lane of ``draft`` with a newer committed value. A restage caused by a manual Save,
    a speech cut, a repair or a heal after a legacy dispatcher breaks the lineage: an old
    pending approval, render retry or intent-review post must not run on top of those.
    """
    if draft.is_head:
        return True
    head = db.execute(
        select(CreatorEditDraft).where(
            CreatorEditDraft.item_id == draft.item_id,
            CreatorEditDraft.variant_key == draft.variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
    ).scalar_one_or_none()
    if head is None or head.base_job_id != draft.base_job_id:
        return False
    draft_payload = (draft.snapshot_json or {}).get("editor_payload")
    draft_lanes = set(lane_view(draft_payload)) if isinstance(draft_payload, dict) else set()
    link: CreatorEditDraft | None = head
    for _ in range(_MAX_RESTAGE_CHAIN):
        marker = restage_marker(link)
        if (
            marker is None
            or marker.get("reason") not in OWN_RENDER_REASONS
            or str(marker.get("of")) != str(link.parent_draft_id)
        ):
            return False
        replaced = (link.snapshot_json or {}).get("replaced_lanes") or []
        if draft_lanes & set(replaced):
            return False
        parent = db.get(CreatorEditDraft, link.parent_draft_id)
        if parent is None:
            return False
        if parent.id == draft.id:
            return True
        link = parent
    return False


def _locked_head(db: Session, item_id: uuid.UUID, variant_key: str) -> CreatorEditDraft | None:
    statement = (
        select(CreatorEditDraft)
        .where(
            CreatorEditDraft.item_id == item_id,
            CreatorEditDraft.variant_key == variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # A writer that held the lock before us demotes its head and inserts a new one. The
    # lock wait re-checks "is_head" on the demoted row and skips it, and our statement
    # snapshot predates the new row, so only a second statement can see the new head.
    for _ in range(2):
        head = db.execute(statement).scalar_one_or_none()
        if head is not None:
            return head
    return None


def _repoint_pinned_draft(job: Job, variant_key: str, head_id: uuid.UUID) -> bool:
    """Point a v2 / empty-state variant's ``editor_draft`` at ``head_id``. True when changed."""
    assembly = job.assembly_plan or {}
    variant = next(
        (
            value
            for value in assembly.get("variants") or []
            if isinstance(value, dict) and str(value.get("variant_id") or "") == variant_key
        ),
        None,
    )
    if (
        variant is None
        or not (variant.get("editor_state") == "empty" or variant.get("editor_state_version") == 2)
        or not isinstance(variant.get("editor_draft"), dict)
        or variant["editor_draft"].get("draft_id") == str(head_id)
    ):
        return False
    # JSON columns are replaced, never mutated in place: an in-place edit is not persisted.
    assembly = copy.deepcopy(assembly)
    target = next(
        value
        for value in assembly["variants"]
        if isinstance(value, dict) and str(value.get("variant_id") or "") == variant_key
    )
    target["editor_draft"] = {**target["editor_draft"], "draft_id": str(head_id)}
    job.assembly_plan = assembly
    return True


# --- lanes ------------------------------------------------------------------------------
#
# A payload names its editor lanes in one of two shapes. A bootstrapped or phone-written head
# holds every lane under ``sections``. A chat draft holds only the lanes the edit changed, flat
# at the top level (``project_editor_draft`` reads only those). The iOS reader takes the flat
# value first and ``sections`` for the rest, so that is the precedence here. A few keys are one
# lane because the phone applies them together (``stagedLaneKeys`` in ``Services.swift``).

NON_LANE_KEYS = frozenset(
    {
        "base_generation",
        "base_lanes",
        "sections",
        "editor_state",
        "editor_state_version",
        "operation",
        "candidate_id",
        "expected_revision",
        "copilot_receipt_ids",
        "learning_temporary",
        "deletions",
        "accepted_suggestion_ids",
        "retry_guided_revision",
        "guided_revision",
        "guided_revision_number",
    }
)
_LANE_MEMBERS = {
    "timeline": ("timeline_slots", "split_screen_sections"),
    "music": ("music_track_id", "music_window", "remove_music"),
    "motion_scenes": ("motion_scenes", "motion_runtime_hash"),
}
_LANE_OF = {member: lane for lane, members in _LANE_MEMBERS.items() for member in members}
GENERATION_BOUND_LANES = frozenset({"timeline"})
# A flat commit-shaped lane whose committed counterpart is spelled differently: the committed
# document holds ``audio_mix`` where a chat draft writes ``mix``, and the caption keys where it
# writes ``caption_meta``. Only the committed side reads through this table.
_COMMITTED_COUNTERPART = {
    "mix": ("audio_mix",),
    "caption_meta": (
        "captions_enabled",
        "caption_style",
        "caption_font",
        "caption_y_frac",
        "caption_size_px",
        "caption_color",
        "caption_highlight_color",
        "caption_stroke_width",
    ),
}
NO_VALUE = ""


def _members(lane: str) -> tuple[str, ...]:
    return _LANE_MEMBERS.get(lane, (lane,))


def _flat_keys(payload: dict[str, Any]) -> list[str]:
    # `remove_music: false` is a Save default the phone always encodes, not a lane.
    return [
        key
        for key, value in payload.items()
        if key not in NON_LANE_KEYS and not (key == "remove_music" and value is False)
    ]


def lane_view(payload: dict[str, Any] | None) -> dict[str, Any]:
    """``{lane: value}`` of an editor payload, whichever shape it is stored in.

    A coupled lane's value is ``{member: value}`` over the members present. Flat keys win over
    ``sections`` for the same member.
    """
    if not isinstance(payload, dict):
        return {}
    sections = payload.get("sections") if isinstance(payload.get("sections"), dict) else {}
    merged = {**sections, **{key: payload[key] for key in _flat_keys(payload)}}
    view: dict[str, Any] = {}
    for key, value in merged.items():
        lane = _LANE_OF.get(key, key)
        if lane in _LANE_MEMBERS:
            view.setdefault(lane, {})[key] = value
        else:
            view[lane] = value
    return view


def flat_lanes(payload: dict[str, Any] | None) -> set[str]:
    """The lanes a payload carries at its top level, i.e. the ones a chat draft changed."""
    if not isinstance(payload, dict):
        return set()
    return {_LANE_OF.get(key, key) for key in _flat_keys(payload)}


def _strip_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def lane_hash(value: object) -> str:
    """Stable hash of one lane's value; ``NO_VALUE`` for a lane that is absent."""
    if value is None:
        return NO_VALUE
    encoded = json.dumps(_strip_none(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def committed_view(job: Any, variant_key: str) -> dict[str, Any]:  # noqa: ANN401
    """The committed variant's lanes, read through the same accessor as a head's."""
    from app.kria.drafts import bootstrap_editor_payload  # noqa: PLC0415

    baseline = variant_baseline(_variant_of(job, variant_key))
    return lane_view(bootstrap_editor_payload(job, variant_key, baseline))


def committed_lane_hash(view: dict[str, Any], lane: str) -> str:
    """Hash of the committed value of ``lane``, through its spelling in the committed document."""
    keys = _COMMITTED_COUNTERPART.get(lane)
    if keys is None:
        return lane_hash(view.get(lane))
    present = {key: view[key] for key in keys if key in view}
    return lane_hash(present or None)


def base_lanes_of(view: dict[str, Any]) -> dict[str, str]:
    """``{lane: hash}`` recorded in a head's payload: the committed lanes it was authored on."""
    recorded = {lane: lane_hash(value) for lane, value in view.items()}
    for lane in _COMMITTED_COUNTERPART:
        digest = committed_lane_hash(view, lane)
        if digest != NO_VALUE:
            recorded[lane] = digest
    return recorded


def stamp_base_lanes(payload: dict[str, Any], job: Any, variant_key: str) -> dict[str, Any]:  # noqa: ANN401
    """``payload`` with ``base_lanes`` set to the committed lanes of ``job``'s variant now.

    ``base_lanes`` lives inside ``editor_payload`` (older readers ignore an unknown key, and the
    phone reads lanes by name). It is inside the hashed snapshot on purpose: the ETag then names
    the base too, so two heads with the same lanes on different bases are different revisions.
    The server always computes it; a value a client sent back is overwritten.
    """
    if payload.get("operation") or _variant_of(job, variant_key) is None:
        # A speech-cut approval's payload is an instruction, not a document.
        return payload
    try:
        return {**payload, "base_lanes": base_lanes_of(committed_view(job, variant_key))}
    except Exception:  # noqa: BLE001 - a head without a recorded base is read by the legacy rule
        log.warning("editor_head_base_lanes_unavailable", variant_key=variant_key, exc_info=True)
        return payload


def _variant_of(job: Any, variant_key: str) -> dict[str, Any] | None:  # noqa: ANN401
    from app.kria.drafts import _variant  # noqa: PLC0415

    return _variant(job, variant_key)


@dataclass
class LaneRebase:
    """A payload moved onto the current generation, and what happened to each lane of it."""

    payload: dict[str, Any]
    outcomes: dict[str, str] = field(default_factory=dict)  # lane -> head | committed | replaced

    @property
    def kept(self) -> bool:
        return "head" in self.outcomes.values()

    @property
    def replaced(self) -> list[str]:
        return sorted(lane for lane, outcome in self.outcomes.items() if outcome == "replaced")


def rebase_payload(
    job: Any,  # noqa: ANN401
    variant_key: str,
    payload: dict[str, Any],
    *,
    same_job: bool = True,
    base_lanes: dict[str, str] | None = None,
    dirty_lanes: frozenset[str] = frozenset(),
    committed_contains_head: bool = False,
    committed_lanes: frozenset[str] = frozenset(),
) -> LaneRebase:
    """Three-way rebase of ``payload`` (a head's editor payload) onto the committed variant.

    Per lane, with h the payload's value, c the committed value now, b the recorded base:

    - the payload lacks the lane: committed;
    - ``base_lanes`` recorded: the payload changed the lane iff hash(h) != b, the committed
      state changed it iff hash(c) != b. Unchanged by the payload: committed. Changed only by
      the payload: the payload's value survives. Changed by both: committed (latest wins), and
      the lane is reported as replaced when the two values differ;
    - nothing recorded (a row written before the base was): a lane at the top level of the
      payload is dirty by construction and survives, and so is a lane named in ``dirty_lanes``;
      a lane that exists only inside a full ``sections`` document cannot be told apart and takes
      the committed value;
    - ``committed_contains_head`` (the approval's render holds the draft) and ``committed_lanes``
      (lanes the writer itself rewrote) take the committed value on every such lane.

    A head from another job names slots that are not on this timeline: nothing of it is replayed.
    An empty-state variant's committed sections are its last non-empty render, not its saved
    state, so its head keeps its own lanes.
    """
    from app.kria.drafts import bootstrap_editor_payload  # noqa: PLC0415

    variant = _variant_of(job, variant_key) or {}
    baseline = variant_baseline(variant)
    committed_sections = bootstrap_editor_payload(job, variant_key, baseline)["sections"]
    committed = lane_view({"sections": committed_sections})
    head_lanes = lane_view(payload) if same_job else {}
    flat = flat_lanes(payload)
    empty_state = variant.get("editor_state") == "empty"

    outcomes: dict[str, str] = {}
    for lane, value in head_lanes.items():
        if empty_state:
            outcomes[lane] = "committed" if committed_contains_head else "head"
        elif committed_contains_head or lane in committed_lanes:
            outcomes[lane] = "committed"
        elif base_lanes is not None:
            recorded = base_lanes.get(lane, NO_VALUE)
            head_changed = lane_hash(value) != recorded
            committed_changed = committed_lane_hash(committed, lane) != recorded
            if not head_changed:
                outcomes[lane] = "committed"
            elif not committed_changed:
                outcomes[lane] = "head"
            elif lane_hash(value) != lane_hash(committed.get(lane)):
                outcomes[lane] = "replaced"
            else:
                outcomes[lane] = "committed"
        else:
            outcomes[lane] = "head" if lane in flat or lane in dirty_lanes else "committed"

    kept = {lane: head_lanes[lane] for lane, outcome in outcomes.items() if outcome == "head"}
    return LaneRebase(
        payload=_write_lanes(payload, kept, committed_sections, baseline, same_job=same_job),
        outcomes=outcomes,
    )


def _write_lanes(
    template: dict[str, Any],
    kept: dict[str, Any],
    committed_sections: dict[str, Any],
    baseline: str,
    *,
    same_job: bool,
) -> dict[str, Any]:
    """``template`` rewritten on ``baseline`` with ``kept`` lanes over the committed ones.

    The result has the shape the template had: a full-``sections`` payload gets the committed
    sections with the kept lanes laid over them (and a flat mirror for a lane that was flat, so
    the phone still sees it staged); a flat payload keeps only its kept lanes at the top level.
    A payload with no lane left is the committed document, so it gets its sections.
    """
    out = {
        key: value
        for key, value in template.items()
        if key in NON_LANE_KEYS and key not in ("sections", "base_generation", "base_lanes")
    }
    flat_present = set(_flat_keys(template))
    members = {
        member: (value[member] if lane in _LANE_MEMBERS else value)
        for lane, value in kept.items()
        for member in _members(lane)
        if lane not in _LANE_MEMBERS or member in value
    }
    for member, value in members.items():
        if member in flat_present:
            out[member] = copy.deepcopy(value)
    if (same_job and isinstance(template.get("sections"), dict)) or not members:
        sections = copy.deepcopy(committed_sections)
        sections.update(copy.deepcopy(members))
        out["sections"] = sections
    out["base_generation"] = baseline
    out["base_lanes"] = base_lanes_of(lane_view({"sections": committed_sections}))
    return out


def rebase_head(
    job: Any,  # noqa: ANN401
    variant_key: str,
    head: Any,  # noqa: ANN401
    *,
    committed_contains_head: bool = False,
    committed_lanes: frozenset[str] = frozenset(),
) -> tuple[Any, LaneRebase]:
    """The head's draft document moved onto the variant's current generation, per lane.

    Returns the document and the per-lane outcomes. ``changes`` and the text diff describe
    pending edits, so they survive only while at least one lane the head changed survives.
    """
    from app.kria.drafts import KriaDraftDocument  # noqa: PLC0415

    old = dict(head.snapshot_json or {})
    old.pop("restage", None)
    old.pop("replaced_lanes", None)
    payload = old.get("editor_payload") or {}
    recorded = payload.get("base_lanes")
    result = rebase_payload(
        job,
        variant_key,
        payload,
        same_job=head.base_job_id == job.id,
        base_lanes=recorded if isinstance(recorded, dict) else None,
        committed_contains_head=committed_contains_head,
        committed_lanes=committed_lanes,
    )
    document = {**old, "editor_payload": result.payload}
    if result.replaced:
        document["replaced_lanes"] = result.replaced
    if not result.kept:
        document["changes"] = []
        for stale in ("editor_text_diff", "client_state_id"):
            document.pop(stale, None)
    return KriaDraftDocument.model_validate(document), result


def rebased_document(
    job: Any,  # noqa: ANN401
    variant_key: str,
    head: Any,  # noqa: ANN401
    *,
    committed_contains_head: bool = False,
    committed_lanes: frozenset[str] = frozenset(),
) -> Any:  # noqa: ANN401
    return rebase_head(
        job,
        variant_key,
        head,
        committed_contains_head=committed_contains_head,
        committed_lanes=committed_lanes,
    )[0]


def effective_lane_hashes(job: Any, variant_key: str, payload: dict[str, Any]) -> dict[str, str]:  # noqa: ANN401
    """``{lane: hash}`` of the document ``payload`` stands for (its lanes over the committed)."""
    view = {**committed_view(job, variant_key), **lane_view(payload)}
    return {lane: lane_hash(value) for lane, value in view.items()}


def restage_editor_head_sync(
    db: Session,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    reason: str,
    committed_contains_head: bool = False,
    committed_lanes: frozenset[str] = frozenset(),
) -> CreatorEditDraft | None:
    """Insert a head revision on the variant's current generation; None when nothing to do.

    Nothing to do: no editor head, or both its stamps already equal the baseline exactly
    (a retried Celery task lands here). The new row records ``restage: {of, reason}`` so a
    restage is never mistaken for a creator's edit, and ``replaced_lanes`` for lanes whose head
    value lost to a newer committed one. Repoints a v2 / empty variant's ``editor_draft`` at the
    new head. The caller commits.
    """
    from app.kria.drafts import _variant, canonical_snapshot  # noqa: PLC0415

    variant = _variant(job, variant_key)
    if variant is None:
        return None
    baseline = variant_baseline(variant)
    head = _locked_head(db, item.id, variant_key)
    if head is None or _payload_base(head) is None:
        return None
    if is_head_on_baseline(job, variant, head):
        _repoint_pinned_draft(job, variant_key, head.id)
        return None

    document, outcome = rebase_head(
        job,
        variant_key,
        head,
        committed_contains_head=committed_contains_head,
        committed_lanes=committed_lanes,
    )
    document = document.model_copy(update={"restage": {"of": str(head.id), "reason": reason}})
    snapshot, snapshot_hash = canonical_snapshot(document)
    latest_revision = int(
        db.execute(
            select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                CreatorEditDraft.item_id == item.id,
                CreatorEditDraft.variant_key == variant_key,
            )
        ).scalar_one()
    )
    head.is_head = False
    db.flush()
    row = CreatorEditDraft(
        creator_id=creator_id,
        thread_id=head.thread_id,
        item_id=item.id,
        variant_key=variant_key,
        base_job_id=job.id,
        base_generation_id=baseline or None,
        draft_revision=latest_revision + 1,
        parent_draft_id=head.id,
        snapshot_json=snapshot,
        snapshot_hash=snapshot_hash,
        is_head=True,
    )
    db.add(row)
    db.flush()
    _repoint_pinned_draft(job, variant_key, row.id)
    log.info(
        "editor_head_restaged",
        reason=reason,
        item_id=str(item.id),
        job_id=str(job.id),
        variant_key=variant_key,
        committed_contains_head=committed_contains_head,
        lanes=outcome.outcomes,
        from_generation=head.base_generation_id,
        to_generation=baseline,
    )
    return row


async def restage_editor_head(
    db: AsyncSession,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    reason: str,
    committed_contains_head: bool = False,
    committed_lanes: frozenset[str] = frozenset(),
) -> CreatorEditDraft | None:
    """Async twin of :func:`restage_editor_head_sync`; the same code runs through ``run_sync``."""

    def run(session: Session) -> CreatorEditDraft | None:
        return restage_editor_head_sync(
            session,
            item=item,
            job=job,
            variant_key=variant_key,
            creator_id=creator_id,
            reason=reason,
            committed_contains_head=committed_contains_head,
            committed_lanes=committed_lanes,
        )

    return await db.run_sync(run)
