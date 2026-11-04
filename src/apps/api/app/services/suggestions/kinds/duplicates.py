"""``duplicates``: keep the better of two (or three) near-identical clips and remove the rest.

Availability reads the ``near_dup_hash`` signal; pairs are decided when the set is served
(``clip_signals.near_dup_hash.duplicate_groups``), only for clips that are still in the edit.
The action is ``remove_clip`` on the slots of the clips to drop: the editor op the copilot parser
and the draft compiler already accept (and the only removal op a pinned tap may run). The card
says which clip stays and why through ``evidence_lines``, built from the measurement, never from
agent words. ``remove_visual_media`` is not used: a visual-media row carries no source media id,
so a duplicate clip cannot be mapped to one.

Needs a ``ready`` render. Before one there is no edit to remove from, and a card must carry an
``Action``, so a first-prompt card cannot be expressed yet.

**Loader input** (``ctx.kind_inputs["duplicates"]``, built where the context is loaded):

* ``needed`` (required, else the kind never offers): media ids the creator's request or the
  approved plan refers to. Every clip a request names, pins, labels or places, a clip-scoped
  brief requirement, and every clip a filming-order requirement orders. A needed clip is never
  removed (KRI-129); when every member of a pair is needed there is nothing to offer.

A clip with speech (``has_speech`` or ``to_camera``) is never removed either: two takes of a
locked-off shot look the same while the lines differ, and the visual hash cannot tell them apart.
* ``capture_times`` (optional): ``{media_id: ISO time}``. Both timed lets near-in-time clips pair
  at the looser threshold; without them the stricter one applies.
* ``exposure`` (optional): ``{media_id: KRI-680 exposure record}`` for the exposure tiebreak.

Exact byte-duplicates are collapsed upstream (``kria/duplicate_uploads.py``); this kind only sees
what that pass left, so the two never report the same pair.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.clip_signals import near_dup_hash
from app.services.clip_signals.registry import SIGNALS_KEY
from app.services.suggestions.builders import PHONE_AND_CLOUD, editor_ops_witness
from app.services.suggestions.context import (
    SuggestionContext,
    Target,
    families,
    live_clips,
    snapshot_digest,
)
from app.services.suggestions.models import (
    Action,
    EditorOpsAction,
    SuggestionKind,
    WitnessResult,
)

INPUT_KEY = "duplicates"
# Conservative format floor: removing a duplicate must leave a real montage, not a single clip.
MIN_REMAINING_SLOTS = 2
# A card names up to three clips (the keeper plus two to remove); a bigger burst is taken in
# steps, and the refresh after the edit offers the rest.
MAX_DROPS = 2
_MAX_OPS = 8
# Equal-quality clips: a take that is clearly longer keeps more of the moment.
LONGER_RATIO = 1.25
LONGER_MIN_GAIN_S = 1.0

_WHY = {
    "en": {
        "sharper": "sharper",
        "better exposed": "better lit",
        "longer": "longer",
        "earlier": "listed first",
        "named": "your request uses it",
        "speech": "it has your speech",
    },
    "tr": {
        "sharper": "daha net",
        "better exposed": "daha iyi ışık",
        "longer": "daha uzun",
        "earlier": "önce eklenen",
        "named": "isteğin onu kullanıyor",
        "speech": "konuşman var",
    },
}


@dataclass(frozen=True)
class Plan:
    keep: str
    drops: tuple[str, ...]
    # sharper | better exposed | longer | earlier | named | speech
    reason: str
    # Indices into the snapshot's FULL slot list (removed rows included): what ``remove_clip``
    # addresses, and what a removal earlier in the same bundle does not shift.
    slot_indices: tuple[int, ...]

    @property
    def members(self) -> frozenset[str]:
        return frozenset((self.keep, *self.drops))


def _inputs(ctx: SuggestionContext) -> Mapping[str, Any]:
    raw = ctx.kind_inputs.get(INPUT_KEY)
    return raw if isinstance(raw, Mapping) else {}


def _needed(ctx: SuggestionContext) -> frozenset[str] | None:
    """None when the loader did not say: nothing can be proven unreferenced, so nothing offers."""
    raw = _inputs(ctx).get("needed")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    return frozenset(str(item) for item in raw if item)


def _speaking(ctx: SuggestionContext) -> frozenset[str]:
    return frozenset(
        str(c["media_id"])
        for c in live_clips(ctx)
        if isinstance(c.get("media_id"), str) and (c.get("has_speech") or c.get("to_camera"))
    )


def _slot_rows(ctx: SuggestionContext) -> list[Any]:
    rows = (ctx.snapshot or {}).get("slots")
    return list(rows) if isinstance(rows, list) else []


def _active_slots(ctx: SuggestionContext) -> dict[int, Mapping[str, Any]]:
    return {
        index: row
        for index, row in enumerate(_slot_rows(ctx))
        if isinstance(row, Mapping) and not row.get("removed")
    }


def _slots_by_media(ctx: SuggestionContext) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for index, row in _active_slots(ctx).items():
        media_id = row.get("media_id")
        if isinstance(media_id, str) and media_id:
            out.setdefault(media_id, []).append(index)
    return out


def _removal_open(ctx: SuggestionContext) -> bool:
    if ctx.state != "ready" or "clip" not in families(ctx):
        return False
    caps = (ctx.snapshot or {}).get("timeline_patch_capabilities")
    return not (isinstance(caps, Mapping) and caps.get("removed") is False)


def _capture_time(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _ordinals(ctx: SuggestionContext) -> dict[str, int]:
    return {
        str(c["media_id"]): c["ordinal"]
        for c in live_clips(ctx)
        if isinstance(c.get("media_id"), str)
        and isinstance(c.get("ordinal"), int)
        and not isinstance(c.get("ordinal"), bool)
    }


def _dup_clips(
    ctx: SuggestionContext, in_edit: Mapping[str, Any], restrict: frozenset[str] | None
) -> list[near_dup_hash.DupClip]:
    times = _inputs(ctx).get("capture_times")
    exposures = _inputs(ctx).get("exposure")
    ordinal = _ordinals(ctx)
    out: list[near_dup_hash.DupClip] = []
    for clip in live_clips(ctx):
        media_id = clip.get("media_id")
        if not isinstance(media_id, str) or media_id not in in_edit or media_id not in ordinal:
            continue
        if restrict is not None and media_id not in restrict:
            continue
        data = (ctx.signals.get(media_id) or {}).get(near_dup_hash.NAME)
        if not isinstance(data, Mapping):
            continue
        # Rebuilt in the stored shape so ``dup_clip`` stays the one reader of a measurement.
        analysis: dict[str, Any] = {
            SIGNALS_KEY: {near_dup_hash.NAME: {"v": near_dup_hash.VERSION, "data": dict(data)}}
        }
        if isinstance(exposures, Mapping) and isinstance(exposures.get(media_id), Mapping):
            analysis["exposure"] = dict(exposures[media_id])
        dup = near_dup_hash.dup_clip(
            media_id,
            str(clip.get("kind") or "video"),
            analysis,
            order=ordinal[media_id],
            capture_time=_capture_time(times.get(media_id)) if isinstance(times, Mapping) else None,
        )
        if dup is not None:
            out.append(dup)
    return out


def _duration(ctx: SuggestionContext, media_id: str) -> float | None:
    for clip in live_clips(ctx):
        value = clip.get("duration_s")
        if (
            clip.get("media_id") == media_id
            and (clip.get("kind") or "video") == "video"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return float(value)
    return None


def _longer(ctx: SuggestionContext, members: Sequence[str]) -> str | None:
    """The clearly longest member, when every member's length is known."""
    lengths = {m: _duration(ctx, m) for m in members}
    if any(v is None for v in lengths.values()):
        return None
    ranked = sorted(members, key=lambda m: -(lengths[m] or 0.0))
    top, second = lengths[ranked[0]] or 0.0, lengths[ranked[1]] or 0.0
    if top >= second * LONGER_RATIO and top - second >= LONGER_MIN_GAIN_S:
        return ranked[0]
    return None


def _plans(ctx: SuggestionContext, restrict: frozenset[str] | None = None) -> list[Plan]:
    """Every card this edit can offer, best first. Empty unless the loader supplied ``needed``."""
    needed = _needed(ctx)
    if needed is None or not _removal_open(ctx):
        return []
    by_media = _slots_by_media(ctx)
    active = _active_slots(ctx)
    ordinal = _ordinals(ctx)
    shielded = needed | _speaking(ctx)
    plans: list[Plan] = []
    for group in near_dup_hash.duplicate_groups(_dup_clips(ctx, by_media, restrict)):
        members = [group.keep, *group.drop]
        protected = [m for m in members if m in shielded]
        droppable = [m for m in members if m not in shielded]
        if not droppable:
            continue  # every copy is needed or speaks: removing any would lose what it carries
        if protected:
            keep = (
                group.keep if group.keep in protected else min(protected, key=ordinal.__getitem__)
            )
            reason = "named" if keep in needed else "speech"
        else:
            keep, reason = group.keep, group.reason
            longer = _longer(ctx, members) if reason == "earlier" else None
            if longer is not None:
                keep, reason = longer, "longer"
        drops = sorted((m for m in droppable if m != keep), key=ordinal.__getitem__)[:MAX_DROPS]
        if not drops:
            continue
        indices = sorted(i for m in drops for i in by_media[m])
        if len(indices) > _MAX_OPS or any(active[i].get("read_only") is True for i in indices):
            continue
        if len(active) - len(indices) < MIN_REMAINING_SLOTS:
            continue
        plans.append(Plan(keep, tuple(drops), reason, tuple(indices)))
    plans.sort(key=lambda p: (-len(p.drops), min(ordinal[m] for m in p.members)))
    return plans


def _availability(ctx: SuggestionContext) -> bool:
    return bool(_plans(ctx))


def _present(ctx: SuggestionContext) -> bool:
    # A removed duplicate leaves no pair in the edit, so availability already says no.
    return False


def _plan_for(ctx: SuggestionContext, targets: Sequence[Target]) -> Plan | None:
    """The plan a card is built for. The named clips must be exactly the clips the card touches."""
    wanted = frozenset(t.media_id for t in targets if t.media_id)
    if not wanted:
        plans = _plans(ctx)
        return plans[0] if plans else None
    if len(wanted) < 2:
        return None
    plan = next((p for p in _plans(ctx, restrict=wanted) if p.members == wanted), None)
    # The card's anchor is the first named clip, and the brief requirement a tap records is
    # scoped to it: it must be a clip that goes, never the keeper.
    if plan is None or targets[0].media_id not in plan.drops:
        return None
    return plan


def _build_action(
    ctx: SuggestionContext, targets: Sequence[Target], copy: str | None
) -> Action | None:
    base = snapshot_digest(ctx.snapshot)
    plan = _plan_for(ctx, targets) if base is not None else None
    if plan is None:
        return None
    return EditorOpsAction(
        ops=[{"op": "remove_clip", "slot_index": i} for i in plan.slot_indices], base_digest=base
    )


def _may_drop_the_pick(
    ctx: SuggestionContext,
    group: near_dup_hash.DupGroup,
    stays: set[str],
    shielded: frozenset[str],
) -> bool:
    """The measurement's pick may go only for a copy that must stay (named or speaking), or for a
    copy that is clearly longer when the measurement could only call it by listing order."""
    if stays & shielded:
        return True
    return group.reason == "earlier" and any(_longer(ctx, [group.keep, m]) == m for m in stays)


def _witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    """Re-prove the removal from the action itself, then dry-run each op through the editor.

    The action may come from the offered-set ring, so the safety rules are checked again here
    rather than trusted from the builder: only ``remove_clip``, never a needed clip, always a
    near-duplicate that stays, and the edit keeps its floor of clips.
    """

    def refuse(reason: str) -> WitnessResult:
        return WitnessResult(ok=False, tier="editor", reason=reason)

    if not isinstance(action, EditorOpsAction):
        return refuse("not_editor_ops")
    needed = _needed(ctx)
    if needed is None or not _removal_open(ctx):
        return refuse("not_removable")
    rows, active = _slot_rows(ctx), _active_slots(ctx)
    indices: list[int] = []
    for op in action.ops:
        index = op.get("slot_index")
        if op.get("op") != "remove_clip" or not isinstance(index, int) or isinstance(index, bool):
            return refuse("op_not_remove_clip")
        if index not in active or active[index].get("read_only") is True:
            return refuse("slot_unavailable")
        indices.append(index)
    if len(set(indices)) != len(indices):
        return refuse("slot_repeated")
    dropped = {rows[i].get("media_id") for i in indices}
    if any(not isinstance(m, str) or not m for m in dropped) or dropped & needed:
        return refuse("removes_a_needed_clip")
    speaking = _speaking(ctx)
    if dropped & speaking:
        return refuse("removes_speech")
    if len(active) - len(indices) < MIN_REMAINING_SLOTS:
        return refuse("below_clip_floor")
    by_media = _slots_by_media(ctx)
    # Every slot of a dropped clip goes: a clip left half-removed is not a removed duplicate.
    if {i for m in dropped for i in by_media.get(m, [])} != set(indices):
        return refuse("partial_removal")
    paired: set[str] = set()
    shielded = needed | speaking
    for group in near_dup_hash.duplicate_groups(_dup_clips(ctx, by_media, None)):
        members = {group.keep, *group.drop}
        stays = members - dropped
        if stays:  # a copy stays
            paired |= members & dropped
            if group.keep in dropped and not _may_drop_the_pick(ctx, group, stays, shielded):
                return refuse("drops_the_better_clip")
    if paired != dropped:
        return refuse("not_a_duplicate")
    return editor_ops_witness(action, ctx)


def evidence_lines(ctx: SuggestionContext, targets: Sequence[Target]) -> list[str]:
    """The card's measured evidence: which clip stays and why. Empty when no card is built."""
    plan = _plan_for(ctx, targets)
    keep = _ordinals(ctx).get(plan.keep) if plan is not None else None
    if plan is None or keep is None:
        return []
    tr = ctx.language.casefold().startswith("tr")
    why = _WHY["tr" if tr else "en"][plan.reason]
    return [f"{keep}. klip kalır: {why}" if tr else f"Keeps clip {keep}: {why}"]


KIND = SuggestionKind(
    kind="duplicates",
    states=frozenset({"ready"}),
    required_signals=("near_dup_hash",),
    availability=_availability,
    already_present=_present,
    build_action=_build_action,
    witness=_witness,
    surfaces=PHONE_AND_CLOUD,
    flag="kria_suggestion_duplicates_enabled",
)
