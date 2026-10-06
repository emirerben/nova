"""One montage plan: a phone montage compiled through the guided plan format (KRI-190).

Before this module a phone montage ran on the plain montage lane, which shows a
single intro hook, orders clips by matcher score, ignores the creator's brief,
and rejects portrait-canvas jobs at default settings (``landscape_fit="fit"``).
The guided plan format already renders per-clip text, honours cut lengths and
has an editor that can revise text after the fact. This module builds that
plan deterministically from what the server already knows about the clips (the
attachment order, the P3 clip facts, the P2 creative brief) and hands it to the
existing guided phone compiler. It is pure: no I/O, no model call.

What it decides, and only this:

* order: capture time when the brief asks for the order the clips were filmed
  in and at least two clips carry a capture time (``services.clip_facts``);
  otherwise the attachment order, recorded honestly in ``ordering``;
* per-clip text: only when the brief asks for it. Every label is grounded in a
  creator-written string, a resolved clip intent, a clip fact or a brief fact.
  A clip with none of those gets no label and the receipt says "partial";
* cut length: a labelled cut lasts at least its reading time
  (``min_display_s``), capped by the clip's own length; an unlabelled cut
  lasts a default fast-cut length;
* title: the creator's confirmed title, else a title written from brief facts
  ("20K Run · Arnavutköy → Eminönü"). Text stays NFC; nothing is folded to
  ASCII. With neither source, the visible title is omitted. A montage never
  takes its title from a Gemini setting;
* Visuals (KRI-217): the item's ready Visuals-pool photos and videos are spread
  evenly between the clips (the montage always opens on a clip) and keep their
  upload order. A photo holds for a fast-cut length, never longer than a beat of
  attention. Runtime v2 has no guided proposal to place them, so without this
  every phone montage with a photo was refused at dispatch.

The output is an ordinary ``EditProposalSnapshot`` (direction ``fast_montage``
with exact ``fast_cuts`` and ``clip_labels``), so the strict guided compiler,
its validators and the phone editor apply unchanged.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.kria.brief_route import (
    END_KEYS,
    START_KEYS,
    first_text,
    fold_text,
    wants_filming_time_text,
    wants_hour_only_text,
)
from app.schemas.edit_proposal import (
    CREATOR_SELECTED_ORIENTATION_REASON,
    MAX_PROPOSAL_DURATION_S,
    ClipLabel,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.schemas.user_song import UserSongPlan
from app.services.clip_facts import (
    capture_time_from_facts,
    display_timezone,
    format_capture_hour,
    order_by_capture_time,
)

FPS = 30
# The reading-time rule: 0.8s to notice the text plus 60ms per character,
# never shorter than a fast cut and never longer than a beat of attention.
READING_BASE_S = 0.8
READING_PER_CHAR_S = 0.06
READING_MIN_S = 1.2
READING_MAX_S = 3.0
# An unlabelled cut keeps the classic fast-montage length.
DEFAULT_CUT_S = 1.2
# A photo has no source length of its own: it holds like a cut and never longer
# than a beat of attention, even when a stated length leaves time to fill.
STILL_MAX_S = READING_MAX_S
# The strict snapshot's shortest video cut (``EditProposalSnapshot``). A clip
# shorter than this is valid only shown whole.
MIN_VIDEO_CUT_S = 0.4
MIN_TOTAL_S = 3.0
# Model- and fact-derived labels stay short; the creator's own words are never cut
# (``ClipLabel`` allows 120).
MAX_LABEL_CHARS = 60
MAX_CREATOR_LABEL_CHARS = 120
# ``EditProposalSnapshot.title`` is required even when no creator-visible title
# exists. Keep its schema placeholder separate from ``opening_title`` so this
# internal label never becomes an on-screen text layer (KRI-255).
SNAPSHOT_FALLBACK_TITLE = "Montage"
_CAPTURE_ORDER_KEYS = frozenset({"capture_time", "chronological", "route", "time", "shot_order"})
_START_KEYS = START_KEYS
_END_KEYS = END_KEYS


def min_display_s(chars: int) -> float:
    """Seconds a label of ``chars`` characters must stay on screen."""
    return round(
        min(READING_MAX_S, max(READING_MIN_S, READING_BASE_S + READING_PER_CHAR_S * chars)), 3
    )


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _cap_label(text: str, provenance: str) -> str:
    limit = MAX_CREATOR_LABEL_CHARS if provenance == "creator" else MAX_LABEL_CHARS
    return text[:limit]


@dataclass(frozen=True)
class UnifiedClip:
    """One phone-bound clip, in attachment order, or one Visuals-pool item.

    A Visual (``lane="asset"``) is a ready ``PlanItemAsset``: ``media_id`` is its
    row id (what the phone binder pins), ``proxy_path`` its pool path, and
    ``duration_s`` is ignored for a photo.
    """

    media_id: str
    proxy_path: str
    generation: str
    duration_s: float
    width: int | None = None
    height: int | None = None
    orientation_degrees: int = 0
    analysis: Mapping[str, Any] = field(default_factory=dict)
    # ``ClipFact.prompt_dict()`` rows (kind, value, provenance). Empty when the
    # account has no clip-facts access.
    facts: tuple[Mapping[str, Any], ...] = ()
    capture_time: datetime | None = None
    lane: str = "clip"
    kind: str = "video"
    # The creator manifest's id (``asset-<uuid>`` for a Visual), which brief
    # scopes and resolved clip intents use. None means ``media_id``.
    manifest_id: str | None = None
    # A Visual's stored aspect; a clip's comes from its width and height.
    aspect: float | None = None
    source_filename: str = ""
    user_context: str = ""
    content_hash: str | None = None

    @property
    def ref_id(self) -> str:
        return self.manifest_id or self.media_id


@dataclass(frozen=True)
class BriefView:
    """What the montage plan needs from a Creative Brief, nothing more."""

    version: int | None = None
    wants_per_clip_text: bool = False
    # ``clip:<media_id>`` scoped literals: the creator's exact words for one clip.
    clip_literals: Mapping[str, str] = field(default_factory=dict)
    # A per-clip requirement that carries a literal ("Km 1 ...") is not a
    # described label; only the scoped ones above are applied per clip.
    wants_order: bool = False
    order_by_capture: bool = False
    # KRI-219: the per-clip text is the hour each clip was filmed, not its place.
    per_clip_text_is_time: bool = False
    # "hh_mm" or "hour" (just the hour, no minutes) for those filming-time labels.
    time_format: str = "hh_mm"
    title_literal: str | None = None
    global_literal: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    target_duration_s: float | None = None


def brief_view(brief: Any) -> BriefView:
    """Reduce a ``CreativeBrief`` (duck-typed, see ``app.kria.brief``) to a view."""
    if brief is None:
        return BriefView()
    wants_text = False
    wants_order = False
    order_capture = False
    wants_time = False
    hour_only = False
    clip_literals: dict[str, str] = {}
    title_literal: str | None = None
    global_literal: str | None = None
    facts: dict[str, Any] = {}
    target: float | None = None
    for req in brief.live():
        for key, value in (req.facts or {}).items():
            if value not in (None, "") and key not in facts:
                facts[str(key)] = value
        if req.kind == "text":
            if wants_filming_time_text(
                req.kind, req.scope, req.literal, req.description, req.facts
            ):
                wants_time = True
            if req.scope == "per_clip" and wants_hour_only_text(req.description, req.literal):
                wants_time = hour_only = True
            if req.scope == "per_clip":
                wants_text = True
            elif req.scope.startswith("clip:") and req.literal:
                wants_text = True
                clip_literals[req.scope.split(":", 1)[1]] = _nfc(req.literal)
            elif req.scope.startswith("clip:"):
                wants_text = True
            elif req.scope == "title" and req.literal:
                title_literal = _nfc(req.literal)
            elif req.scope == "global" and req.literal and global_literal is None:
                global_literal = _nfc(req.literal)
        elif req.kind == "order":
            wants_order = True
            key = str((req.facts or {}).get("key") or (req.facts or {}).get("by") or "")
            if key.casefold() in _CAPTURE_ORDER_KEYS:
                order_capture = True
        elif req.kind == "timing":
            raw = (req.facts or {}).get("duration_s")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
                target = float(raw)
    return BriefView(
        version=int(getattr(brief, "version", 0) or 0) or None,
        wants_per_clip_text=wants_text,
        clip_literals=clip_literals,
        wants_order=wants_order,
        order_by_capture=order_capture,
        per_clip_text_is_time=wants_time,
        time_format="hour" if hour_only else "hh_mm",
        title_literal=title_literal,
        global_literal=global_literal,
        facts=facts,
        target_duration_s=target,
    )


_first = first_text


def _has_dotted_i(text: str) -> bool:
    return any(ch in "iıİI" for ch in text)


def title_from_facts(facts: Mapping[str, Any]) -> str | None:
    """A title written from brief facts, e.g. ``20K Run · Arnavutköy → Eminönü``.

    Only what the creator stated goes in; a missing part is left out, never
    invented. Returns None when the brief carries nothing to title with.
    """
    distance = facts.get("distance_km", facts.get("distance"))
    headline = ""
    if isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance > 0:
        headline = f"{distance:g}K"
    elif isinstance(distance, str) and _nfc(distance):
        text = _nfc(distance)
        # str.upper() turns Turkish "i" into an ASCII "I": leave those as written.
        headline = text.upper() if len(text) <= 5 and not _has_dotted_i(text) else text
    activity = _first(facts, ("activity", "event", "sport"))
    if activity:
        if not _has_dotted_i(activity[:1]):
            activity = activity[0].upper() + activity[1:]
        headline = f"{headline} {activity}".strip()
    start = _first(facts, _START_KEYS)
    end = _first(facts, _END_KEYS)
    route = f"{start} → {end}" if start and end else ""
    parts = [part for part in (headline, route) if part]
    if not parts:
        return None
    return " · ".join(parts)[:100]


def _place_label(value: str) -> str:
    """The most specific part of a geocoded place ("Sarıyer, Istanbul, Türkiye")."""
    return _nfc(value.split(",", 1)[0])


def _fact_label(clip: UnifiedClip) -> tuple[str, str, bool] | None:
    """(text, fact_kind, inferred) for the best fact on a clip, or None."""
    by_kind: dict[str, Mapping[str, Any]] = {}
    for fact in clip.facts:
        kind = str(fact.get("kind") or "")
        if kind and kind not in by_kind and _nfc(fact.get("value")):
            by_kind[kind] = fact
    if "creator" in by_kind:
        return _cap_label(_nfc(by_kind["creator"]["value"]), "creator"), "creator", False
    if "landmark" in by_kind:
        fact = by_kind["landmark"]
        return (
            _nfc(fact["value"])[:MAX_LABEL_CHARS],
            "landmark",
            str(fact.get("provenance")) == "inferred",
        )
    if "place" in by_kind:
        label = _place_label(str(by_kind["place"]["value"]))
        if label:
            return label[:MAX_LABEL_CHARS], "place", False
    return None


def _endpoint_record(clip: UnifiedClip) -> dict[str, Any]:
    """Where one endpoint clip was filmed: its geocoded place and inferred landmark.

    ``places`` keeps the full geocode ("Kadıköy, İstanbul, Türkiye") so a route name can
    match any part of it; ``label`` is the short text the creator would recognise.
    """
    places: list[dict[str, str]] = []
    label = ""
    for fact in clip.facts:
        kind = str(fact.get("kind") or "")
        value = _nfc(fact.get("value"))
        if kind not in ("place", "landmark") or not value:
            continue
        places.append(
            {
                "text": value[:120],
                "kind": kind,
                "provenance": str(fact.get("provenance") or ""),
            }
        )
        if kind == "place" and not label:
            label = _place_label(value)[:MAX_LABEL_CHARS]
    if not label:
        label = next((row["text"] for row in places if row["kind"] == "landmark"), "")[
            :MAX_LABEL_CHARS
        ]
    return {"media_id": clip.media_id, "label": label, "places": places}


def _endpoint_places(ordered: Sequence[UnifiedClip]) -> dict[str, Any]:
    """The first and last ordered clips' place facts (needs at least two clips)."""
    if len(ordered) < 2:
        return {}
    first, last = _endpoint_record(ordered[0]), _endpoint_record(ordered[-1])
    if not first["places"] and not last["places"]:
        return {}
    return {"first": first, "last": last}


@dataclass
class UnifiedMontagePlan:
    snapshot: EditProposalSnapshot
    clip_ids: list[str]
    title: str | None
    title_source: str
    label_clip_ids: list[str]
    dropped_label_clip_ids: list[str]
    short_label_clip_ids: list[str]
    ordering_basis: str
    ordering_fallback_clip_ids: list[str]
    duration_s: float
    brief_version: int | None
    wants_per_clip_text: bool
    # media_id -> why its label was left off ("repeat": same text as the previous
    # kept label; "no_fact": nothing grounded to write). Ids only, never text.
    dropped_label_reasons: dict[str, str] = field(default_factory=dict)
    # The route the creator stated, and where the first/last clips were filmed
    # (KRI-208): what the receipt needs to notice a reversed route.
    route: dict[str, str] = field(default_factory=dict)
    endpoint_places: dict[str, Any] = field(default_factory=dict)
    # The Visuals-pool ids among ``clip_ids`` (KRI-217), in plan order.
    visual_ids: list[str] = field(default_factory=list)
    # KRI-296: the clips the creator described and gave exact text for, in plan
    # order. Empty when the text was not matched to described shots.
    label_scope_clip_ids: list[str] = field(default_factory=list)
    # KRI-282: what each requested clip intent (group / sport label / chapter text)
    # actually did in this plan, for an honest receipt. Empty without clip intents.
    intent_outcomes: list[dict[str, Any]] = field(default_factory=list)
    # KRI-282: the creator's answer to a chronological-vs-grouped conflict that this
    # plan followed ("group_first" | "chronological"); None when none was asked.
    ordering_choice: str | None = None
    # Zone the filming hours were printed in, "" when the labels are not hours.
    label_timezone: str = ""
    label_timezone_basis: str = ""
    # KRI-374: the creator's own song for this montage. ``user_song`` is also on
    # ``snapshot``; ``song_receipt`` is the ids-only account of what the song
    # planner did (window, snapped beats, what was left out and why). Both stay
    # empty without a song so every earlier record is unchanged.
    user_song: UserSongPlan | None = None
    song_receipt: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        """The small, JSON-safe receipt persisted beside the guided snapshot."""
        # Only a montage with Visuals names them: every earlier record stays as is.
        visuals = {"visual_ids": list(self.visual_ids)} if self.visual_ids else {}
        # Likewise the closing title and the described-shot scope (KRI-296).
        closing = self.snapshot.closing_title
        closing_title = {"closing_title": closing} if closing else {}
        scope = (
            {"label_scope_clip_ids": list(self.label_scope_clip_ids)}
            if self.label_scope_clip_ids
            else {}
        )
        outcomes = {"intent_outcomes": list(self.intent_outcomes)} if self.intent_outcomes else {}
        choice = {"ordering_choice": self.ordering_choice} if self.ordering_choice else {}
        zone = (
            {
                "label_timezone": self.label_timezone,
                "label_timezone_basis": self.label_timezone_basis,
            }
            if self.label_timezone
            else {}
        )
        song = {"user_song": dict(self.song_receipt)} if self.song_receipt else {}
        return {
            "version": 1,
            "brief_version": self.brief_version,
            "clip_ids": list(self.clip_ids),
            "title": self.title,
            "title_source": self.title_source,
            "duration_s": self.duration_s,
            "wants_per_clip_text": self.wants_per_clip_text,
            "labels": [
                {
                    "media_id": label.media_id,
                    "text": label.text,
                    "provenance": label.provenance,
                    "fact_kind": label.fact_kind,
                    "inferred": label.inferred,
                }
                for label in (self.snapshot.clip_labels or [])
            ],
            "dropped_label_clip_ids": list(self.dropped_label_clip_ids),
            "dropped_label_reasons": dict(self.dropped_label_reasons),
            "short_label_clip_ids": list(self.short_label_clip_ids),
            "ordering_basis": self.ordering_basis,
            "ordering_fallback_clip_ids": list(self.ordering_fallback_clip_ids),
            "route": dict(self.route),
            "endpoint_places": dict(self.endpoint_places),
            **visuals,
            **closing_title,
            **scope,
            **outcomes,
            **choice,
            **zone,
            **song,
        }

    def guided_edit(self, *, generation_attempt_id: str | None = None) -> dict[str, Any]:
        """The immutable ``assembly_plan["guided_edit"]`` payload for this plan."""
        snapshot = self.snapshot
        payload: dict[str, Any] = {
            "proposal_version": 1,
            "media_digest": canonical_media_digest(snapshot.media, snapshot.narration),
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                {
                    "lane": ref.lane,
                    "media_id": ref.media_id,
                    "gcs_path": ref.gcs_path,
                    "generation": ref.generation,
                    "kind": ref.kind,
                }
                for ref in snapshot.media
            ],
        }
        # No attempt id is minted: a thread session that dispatched without a
        # guided proposal has none, and the render projection drops a job whose
        # attempt id differs from the session's (chat would stay "preparing").
        if generation_attempt_id:
            payload["generation_attempt_id"] = generation_attempt_id
        return payload


def ordered_ids(clips: Sequence[UnifiedClip]) -> list[str]:
    return [clip.media_id for clip in clips]


_MIN_UNLABELLED_FRAMES = int(round(0.8 * FPS))


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _grow(frames: list[int], ceilings: Sequence[int], target: int) -> int:
    """Add one frame at a time, round-robin, to cuts below their ceiling."""
    total = sum(frames)
    while total < target:
        grew = False
        for index in range(len(frames)):
            if total >= target:
                break
            if frames[index] < ceilings[index]:
                frames[index] += 1
                total += 1
                grew = True
        if not grew:
            break
    return total


def _shrink(frames: list[int], labelled: Sequence[bool], total: int, target: int) -> int:
    """Take one frame at a time from unlabelled cuts down to the shortest cut."""
    while total > target:
        shrank = False
        for index in range(len(frames)):
            if total <= target:
                break
            if not labelled[index] and frames[index] > _MIN_UNLABELLED_FRAMES:
                frames[index] -= 1
                total -= 1
                shrank = True
        if not shrank:
            break
    return total


def _capacity_frames(clip: UnifiedClip) -> int:
    if clip.kind == "image":
        return int(round(STILL_MAX_S * FPS))
    return max(1, int(math.floor((float(clip.duration_s) + 0.001) * FPS + 1e-6)))


def _window_start_s(clip: UnifiedClip, duration_s: float) -> float:
    """Start of the cut inside the clip: its strongest analysed moment, else 0."""
    start = 0.0
    moments = clip.analysis.get("best_moments") if isinstance(clip.analysis, Mapping) else None
    best_energy = -1.0
    for moment in moments if isinstance(moments, list) else []:
        if not isinstance(moment, dict):
            continue
        try:
            moment_start = float(moment.get("start_s"))
        except (TypeError, ValueError):
            continue
        energy = moment.get("energy", 0)
        energy_value = float(energy) if isinstance(energy, (int, float)) else 0.0
        if math.isfinite(moment_start) and energy_value > best_energy:
            best_energy = energy_value
            start = moment_start
    latest = max(0.0, float(clip.duration_s) - duration_s)
    return round(min(max(0.0, start), latest), 3)


def _story_beats(cuts: Sequence[FastMontageCut]) -> list[StoryBeat]:
    beats: list[StoryBeat] = []
    for index in range(0, len(cuts), 4):
        group = cuts[index : index + 4]
        media_ids: list[str] = []
        for cut in group:
            if cut.media_id not in media_ids:
                media_ids.append(cut.media_id)
        beats.append(
            StoryBeat(
                beat_id=f"fast-beat-{index // 4 + 1}",
                topic="Fast montage",
                thought="",
                thought_source="ai_draft",
                media_ids=media_ids,
                layout="fullscreen",
                duration_s=max(
                    1.0, min(MAX_PROPOSAL_DURATION_S, sum(c.output_duration_s for c in group))
                ),
            )
        )
    return beats


def _creator_ordered(
    clips: Sequence[UnifiedClip], creator_order: Sequence[int]
) -> list[UnifiedClip]:
    """The creator's pinned order (indices into ``clips``); unlisted clips follow."""
    picked: list[int] = []
    for index in creator_order:
        if isinstance(index, int) and 0 <= index < len(clips) and index not in picked:
            picked.append(index)
    picked.extend(i for i in range(len(clips)) if i not in picked)
    return [clips[i] for i in picked]


def _order(
    clips: Sequence[UnifiedClip], view: BriefView, creator_order: Sequence[int] = ()
) -> tuple[list[UnifiedClip], str, list[str]]:
    attachment_ids = [clip.media_id for clip in clips]
    if not view.order_by_capture:
        if creator_order:
            # A creator-reordered timeline is what a revision keeps; only an
            # explicit capture-time ask in the brief overrides it.
            return _creator_ordered(clips, creator_order), "creator_order", []
        return list(clips), "attachment", []
    times = {clip.media_id: clip.capture_time for clip in clips if clip.capture_time is not None}
    result = order_by_capture_time(attachment_ids, times)
    by_id = {clip.media_id: clip for clip in clips}
    return (
        [by_id[media_id] for media_id in result.ordered_ids],
        result.basis,
        list(result.fallback_ids),
    )


def _group_first(
    ordered: Sequence[UnifiedClip], owners: Mapping[str, list[str]]
) -> list[UnifiedClip]:
    """Group the clips, chronological inside each group (KRI-282 ``group_first``).

    ``ordered`` is the order the montage would otherwise use (capture time). Each clip
    belongs to the ONE group that names it; a clip in several groups is ambiguous and
    counts as ungrouped, like in the receipt. Groups play as one block each, blocks
    ordered by the earliest position of any of their clips, clips inside a block in the
    incoming order. Ungrouped clips keep the slot they had in ``ordered`` (an opening
    shot of the pub stays the opening shot); only grouped clips are re-seated, into
    the slots grouped clips already held.
    """
    group_of = {
        ref: fold_text(names[0]) for ref, names in owners.items() if len(names) == 1 and names[0]
    }
    slots = [i for i, clip in enumerate(ordered) if clip.ref_id in group_of]
    blocks: dict[str, list[UnifiedClip]] = {}
    for i in slots:
        blocks.setdefault(group_of[ordered[i].ref_id], []).append(ordered[i])
    sequence = [clip for block in blocks.values() for clip in block]
    result = list(ordered)
    for slot, clip in zip(slots, sequence, strict=True):
        result[slot] = clip
    return result


def _sequence_intents(
    strategy: Mapping[str, Any], enabled: bool
) -> list[tuple[str | None, str, list[str], str]]:
    """The creator's stated sequence: (position, name, member ids, status), listed order.

    Only ``order`` intents with a ``first`` / ``last`` position, or no position and no
    ``order_by`` ("then the beach volleyball"), describe a sequence of groups. A basis
    order (``order_by``: capture time / route) is the brief's, not this one's.
    """
    if not enabled:
        return []
    rows: list[tuple[str | None, str, list[str], str]] = []
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "order":
            continue
        if intent.get("order_by") or intent.get("placeholder"):
            continue
        position = intent.get("position")
        if position not in (None, "first", "last"):
            continue
        status = str(intent.get("status") or "resolved")
        members = _members(intent) if status == "resolved" else []
        rows.append((position, _nfc(intent.get("attribute")), members, status))
    return rows


def _apply_sequence(
    ordered: Sequence[UnifiedClip], rows: Sequence[tuple[str | None, str, list[str], str]]
) -> list[UnifiedClip]:
    """Seat the described groups: ``first`` ones lead, ``last`` ones close, the rest follow.

    A group is the clips the server matched to the creator's words (clip facts), kept in
    the order they already had. A clip named by two groups belongs to the first one. Clips
    no group names keep their relative order between the leading and the closing groups.
    """
    claimed: set[str] = set()
    buckets: dict[str, list[UnifiedClip]] = {"first": [], "mid": [], "last": []}
    refs = {clip.ref_id for clip in ordered}
    for position, _name, members, _status in rows:
        wanted = {m for m in members if m in refs and m not in claimed}
        claimed |= wanted
        buckets[position or "mid"].extend(clip for clip in ordered if clip.ref_id in wanted)
    if not claimed:
        return list(ordered)
    rest = [clip for clip in ordered if clip.ref_id not in claimed]
    return [*buckets["first"], *buckets["mid"], *rest, *buckets["last"]]


def _sequence_outcomes(
    rows: Sequence[tuple[str | None, str, list[str], str]], ordered: Sequence[UnifiedClip]
) -> list[dict[str, Any]]:
    """Did each described group land where the creator said? Read off the finished order."""
    spot = {clip.ref_id: i for i, clip in enumerate(c for c in ordered if c.lane == "clip")}
    named = {m for _p, _n, members, _s in rows for m in members if m in spot}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, name, members, status in rows:
        label = f"{position or 'then'}: {name}" if name else "the order you described"
        row = {"op": "order", "name": label}
        mine = [m for m in members if m in spot and m not in seen]
        if status != "resolved":
            out.append({**row, "status": "not_possible", "reason": "I couldn't tell which clips"})
            continue
        if not mine:
            reason = (
                "its clips are already in an earlier group"
                if any(m in seen for m in members)
                else "I found no clips of it"
            )
            out.append({**row, "status": "not_possible", "reason": reason})
            continue
        seen.update(mine)
        ahead = {spot[m] for m in mine}
        others = [i for r, i in spot.items() if i not in ahead and r not in named]
        met = (
            position is None
            or not others
            or (position == "first" and max(ahead) < min(others))
            or (position == "last" and min(ahead) > max(others))
        )
        out.append(
            {
                **row,
                "status": "met" if met else "partial",
                "reason": None if met else "the clips did not end up there",
            }
        )
    return out


def _ordering_choice(strategy: Mapping[str, Any], enabled: bool) -> str | None:
    choice = strategy.get("ordering_choice") if enabled else None
    return choice if choice in ("group_first", "chronological") else None


def _scatter(clips: Sequence[UnifiedClip], visuals: Sequence[UnifiedClip]) -> list[UnifiedClip]:
    """Spread ``visuals`` evenly between ``clips``; the first cut stays a clip.

    Visual ``j`` of ``m`` goes after ``round((j + 1) * n / (m + 1))`` of the
    ``n`` clips (at least one), so 6 clips and 2 photos read C C P C C P C C.
    """
    n, m = len(clips), len(visuals)
    after = [max(1, int(math.floor((j + 1) * n / (m + 1) + 0.5))) for j in range(m)]
    merged: list[UnifiedClip] = []
    pending = list(zip(after, visuals, strict=True))
    for index, clip in enumerate(clips, start=1):
        merged.append(clip)
        while pending and pending[0][0] <= index:
            merged.append(pending.pop(0)[1])
    merged.extend(visual for _slot, visual in pending)
    return merged


def selected_visual_ids(strategy: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Manifest ids of the media an explicit ``selected`` scope names, else None.

    None means every ready Visual. The guided strategy leaves
    ``selected_media_ids`` empty when it takes everything
    (``normalize_creator_strategy_media``), so only a non-empty explicit subset
    narrows the Visuals a unified montage places.
    """
    if not isinstance(strategy, Mapping) or strategy.get("media_scope") != "selected":
        return None
    ids = strategy.get("selected_media_ids")
    if not isinstance(ids, list) or not ids:
        return None
    return frozenset(str(media_id) for media_id in ids)


# ── background song (KRI-374) ────────────────────────────────────────────────
# A beat prefers to be hit exactly; a boundary no beat can serve (sparse beats,
# a reading-time floor in the way) keeps its nominal place at this cost, so a
# beat within this distance always wins over leaving the cut where it was.
_BEAT_MISS_COST_MS = 600
# Beats farther than this from a boundary's nominal place are not considered:
# a cut that moves more than a second and a half is a different edit.
_BEAT_SEARCH_MS = 1500
_MIN_UNLABELLED_MS = 800


def _snap_boundaries_to_beats(
    nominal_ms: Sequence[int],
    lo_ms: Sequence[int],
    hi_ms: Sequence[int],
    beats_ms: Sequence[int],
) -> tuple[list[int], int]:
    """Cut durations (ms) whose internal boundaries sit on ``beats_ms`` where possible.

    ``nominal_ms`` is the plan's own cut lengths; ``lo_ms``/``hi_ms`` bound each cut
    (a label's reading time, a clip's length). The total never changes and every
    cut stays inside its bounds; the all-nominal layout is always feasible, so
    this cannot fail. Returns the durations and how many internal boundaries
    landed on a beat.
    """
    count = len(nominal_ms)
    total = sum(nominal_ms)
    cumulative: list[int] = []
    running = 0
    for value in nominal_ms:
        running += value
        cumulative.append(running)
    options: list[list[tuple[int, int]]] = []
    for index in range(count - 1):
        nominal = cumulative[index]
        costs: dict[int, int] = {nominal: _BEAT_MISS_COST_MS}
        for beat in beats_ms:
            if abs(beat - nominal) <= _BEAT_SEARCH_MS and 0 < beat < total:
                costs[beat] = min(costs.get(beat, _BEAT_MISS_COST_MS), abs(beat - nominal))
        options.append(sorted(costs.items()))
    options.append([(total, 0)])
    # state: boundary position -> (cost, previous boundary) per boundary index
    layers: list[dict[int, tuple[int, int | None]]] = [{0: (0, None)}]
    for index in range(count):
        layer: dict[int, tuple[int, int | None]] = {}
        for boundary, boundary_cost in options[index]:
            best: tuple[int, int | None] | None = None
            for previous, (previous_cost, _back) in layers[-1].items():
                if lo_ms[index] <= boundary - previous <= hi_ms[index]:
                    cost = previous_cost + boundary_cost
                    if best is None or cost < best[0]:
                        best = (cost, previous)
            if best is not None:
                layer[boundary] = best
        layers.append(layer)
    boundary = total
    path = [total]
    for index in range(count, 0, -1):
        previous = layers[index][boundary][1]
        assert previous is not None
        path.append(previous)
        boundary = previous
    path.reverse()
    durations = [path[i + 1] - path[i] for i in range(count)]
    on_beat = sum(1 for value in path[1:-1] if value in set(beats_ms))
    return durations, on_beat


def _song_line_dicts(lines: Sequence[Any] | None) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for line in lines or ():
        start = line.get("start_s") if isinstance(line, Mapping) else getattr(line, "start_s", None)
        end = line.get("end_s") if isinstance(line, Mapping) else getattr(line, "end_s", None)
        try:
            rows.append({"start_s": float(start), "end_s": float(end)})
        except (TypeError, ValueError):
            continue
    return rows


def plan_unified_montage(
    clips: Sequence[UnifiedClip],
    view: BriefView | None = None,
    *,
    strategy: Mapping[str, Any] | None = None,
    clip_intents_enabled: bool = False,
    font_covers: Callable[[str, str], bool] | None = None,
    creator_order: Sequence[int] = (),
    visuals: Sequence[UnifiedClip] = (),
    song_beats: Sequence[float] | None = None,
    song_lines: Sequence[Any] | None = None,
    song_duration_s: float | None = None,
    song_plan_item_id: str | None = None,
    song_generation: int | None = None,
    output_orientation: str | None = None,
) -> UnifiedMontagePlan:
    """Build the guided fast-montage plan for ``clips`` (attachment order).

    Background song (KRI-374): with ``song_duration_s`` set, the creator's own
    song is the music bed. The total is capped at the song's length, the window
    is the best section of the song for that length
    (``music_recipe.auto_best_section``), and every cut boundary is pre-snapped
    to that window's beats, never closer than a label's reading time. Cuts are
    emitted with ``beat_align=False``: ``compile_execution_plan`` only nudges
    cuts of 0.4-1.2 s (``_fast_montage_output_windows``), so the snap is done
    here, once, and the compiler leaves the boundaries alone. ``song_beats`` /
    ``song_lines`` are the song analysis' beat times and lyric lines
    (``{start_s, end_s}``); the plan carries a ``UserSongPlan(mode="background")``
    on ``plan.user_song`` and on the snapshot. Without ``song_duration_s`` the
    output is byte-identical to before.

    ``strategy`` is the serialized Main Creator ``CreativeStrategy`` the job was
    approved with. Only its creator-confirmed copy is read: ``opening_title``,
    ``closing_title``, ``shot_labels``, ``font_family``, ``text_color``,
    ``image_layout`` and, when ``clip_intents_enabled``, the server-verified
    ``resolved_clip_intents``. ``creator_order`` is the server-pinned order of
    the previous timeline (indices into ``clips``); see ``_order``.
    ``font_covers(family, text)`` says whether a bundled font has a glyph for
    every character of ``text`` (see ``skia_font_covers``); without it the
    default typography is used as is. ``visuals`` are the item's ready
    Visuals-pool items (``lane="asset"``) in upload order; see ``_scatter``.
    ``output_orientation`` (KRI-306) is the creator's explicit finished-video
    shape: ``"portrait"``/``"landscape"`` pins the canvas with the reason "The
    creator selected this output format"; ``None`` keeps the snapshot's own
    aspect-vote inference byte-identical.
    """
    view = view or BriefView()
    strategy = strategy or {}
    if song_duration_s is not None and (song_plan_item_id is None or song_generation is None):
        raise ValueError("a song montage needs the song's plan item and generation")
    if not clips:
        raise ValueError("a montage needs at least one clip")
    if any(visual.lane != "asset" for visual in visuals):
        raise ValueError("montage visuals must come from the Visuals pool")
    if len({clip.media_id for clip in (*clips, *visuals)}) != len(clips) + len(visuals):
        raise ValueError("montage clips must have unique media identities")
    ordered, basis, fallback_ids = _order(clips, view, creator_order)
    choice = _ordering_choice(strategy, clip_intents_enabled)
    if choice == "group_first":
        # The creator chose grouping over strict filming order (KRI-282). Blocks are
        # built from the chronological order above, so each block stays chronological.
        ordered = _group_first(ordered, _group_owners(strategy))
    # KRI-458: "start with X, then Y, end at Z" -- the creator's own sequence of
    # described groups, matched to clips by the server, outranks every other order.
    sequence = _sequence_intents(strategy, clip_intents_enabled)
    if sequence:
        ordered = _apply_sequence(ordered, sequence)
    ordered = _scatter(ordered, visuals)
    if view.order_by_capture:
        # Visuals carry no capture time: they keep their spread slot, and the
        # receipt says their place is not the filmed order.
        fallback_ids = [*fallback_ids, *(v.media_id for v in visuals if v.capture_time is None)]

    # ── labels ───────────────────────────────────────────────────────────────
    labels_requested = bool(
        view.wants_per_clip_text
        or strategy.get("shot_labels")
        or _intent_labels(strategy, clip_intents_enabled)
    )
    positional = [_nfc(x) for x in (strategy.get("shot_labels") or []) if _nfc(x)]
    intent_labels = _intent_labels(strategy, clip_intents_enabled)
    # KRI-296: the creator described each shot and gave its text, and the server
    # matched each description to a clip. That match decides where the text goes,
    # never the text's place in the creator's list (shots are rarely listed in
    # upload order). The clips they did not describe then get no text at all.
    described = _described_shot_labels(strategy, clip_intents_enabled)
    start_fact = _first(view.facts, _START_KEYS)
    end_fact = _first(view.facts, _END_KEYS)

    labels: dict[str, ClipLabel] = {}
    dropped: list[str] = []
    dropped_reasons: dict[str, str] = {}
    previous_kept: str | None = None  # folded text of the last label that stayed
    # "Add the hour to each video" (KRI-219): print the filming hour, or leave the
    # clip unlabelled and say so. A place name is never substituted for it.
    hour_zone, hour_basis = display_timezone(
        [clip.facts for clip in ordered] if view.per_clip_text_is_time else []
    )
    for index, clip in enumerate(ordered):
        chosen: tuple[str, str, str | None, bool] | None = None  # text, provenance, kind, inferred
        if clip.ref_id in view.clip_literals:
            chosen = (view.clip_literals[clip.ref_id], "creator", None, False)
        elif clip.ref_id in described:
            chosen = (described[clip.ref_id], "creator", None, False)
        elif not described and index < len(positional):
            chosen = (positional[index], "creator", None, False)
        elif clip.ref_id in intent_labels:
            text, creator_text = intent_labels[clip.ref_id]
            chosen = (text, "creator" if creator_text else "fact", None, not creator_text)
        elif described:
            pass
        elif view.wants_per_clip_text and view.per_clip_text_is_time:
            moment = clip.capture_time or capture_time_from_facts(clip.facts)
            if moment is not None:
                chosen = (
                    format_capture_hour(moment, hour_zone, view.time_format),
                    "fact",
                    "capture_time",
                    False,
                )
        elif view.wants_per_clip_text and not intent_labels:
            # KRI-282: when the creator's own intents decide which clips carry text,
            # a clip they did not name stays unlabelled. A place or landmark taken
            # from clip facts is never printed in their place ("Wandsworth").
            fact = _fact_label(clip)
            if fact is not None:
                chosen = (fact[0], "creator" if fact[1] == "creator" else "fact", fact[1], fact[2])
            elif index == 0 and start_fact:
                chosen = (start_fact[:MAX_LABEL_CHARS], "brief", "start", False)
            elif index == len(ordered) - 1 and end_fact and len(ordered) > 1:
                chosen = (end_fact[:MAX_LABEL_CHARS], "brief", "end", False)
        if chosen is None:
            if labels_requested and not described and not intent_labels:
                dropped.append(clip.media_id)
                dropped_reasons[clip.media_id] = (
                    "no_capture_time" if view.per_clip_text_is_time else "no_fact"
                )
            continue
        text = _cap_label(_nfc(chosen[0]), chosen[1])
        if not text:
            dropped.append(clip.media_id)
            dropped_reasons[clip.media_id] = "no_fact"
            continue
        folded = fold_text(text)
        if chosen[1] != "creator" and chosen[2] != "capture_time" and folded == previous_kept:
            # Three clips on one bridge would read "Bosphorus Strait" three times: a
            # label stays only when it adds information. The creator's own words are
            # never dropped, however often they repeat them.
            dropped.append(clip.media_id)
            dropped_reasons[clip.media_id] = "repeat"
            continue
        previous_kept = folded
        labels[clip.media_id] = ClipLabel(
            media_id=clip.media_id,
            text=text,
            provenance=chosen[1],  # type: ignore[arg-type]
            fact_kind=chosen[2],
            inferred=chosen[3],
            min_display_s=min_display_s(len(text)),
        )

    # ── title and typography ─────────────────────────────────────────────────
    title, title_source = _title(strategy, view)
    closing = _nfc(strategy.get("closing_title")) or None
    requested_font = strategy.get("font_family")
    requested_font = requested_font if isinstance(requested_font, str) and requested_font else None
    labelled_before = set(labels)
    family, title, closing, labels = _fit_typography(
        font_covers, requested_font, title, closing, labels
    )
    for media_id in ordered_ids(ordered):
        if media_id in labelled_before - set(labels):
            dropped.append(media_id)
            dropped_reasons[media_id] = "no_fact"
    if not title:
        title_source = "none"

    # ── cut lengths ──────────────────────────────────────────────────────────
    wanted: list[int] = []
    capacity: list[int] = []
    short: list[str] = []
    for clip in ordered:
        label = labels.get(clip.media_id)
        want_s = label.min_display_s if label is not None else DEFAULT_CUT_S
        want = int(math.ceil(want_s * FPS - 1e-9))
        cap = _capacity_frames(clip)
        if label is not None and cap < want:
            short.append(clip.media_id)
        wanted.append(min(want, cap))
        capacity.append(cap)
    labelled = [clip.media_id in labels for clip in ordered]
    floor_frames = int(math.ceil(MIN_TOTAL_S * FPS))
    target_s = view.target_duration_s or _positive_number(strategy.get("target_duration_s"))
    target_frames = max(floor_frames, int(round(target_s * FPS))) if target_s else floor_frames
    # A creator-stated length is honoured even when it needs cuts longer than a
    # beat of attention (one long clip, a day vlog); a default fast montage is not.
    growth_ceiling = (
        list(capacity) if target_s else [min(cap, int(READING_MAX_S * FPS)) for cap in capacity]
    )
    total = _grow(wanted, growth_ceiling, target_frames)
    if total < floor_frames:
        # Too short to be a video at all: use every frame the clips have.
        total = _grow(wanted, capacity, floor_frames)
        if total < floor_frames:
            raise ValueError("these clips are too short to make a montage")
    if total > target_frames and target_s:
        # Longer than the creator asked for: only unlabelled cuts may shrink,
        # and never below the shortest cut a montage should have.
        total = _shrink(wanted, labelled, total, max(target_frames, floor_frames))
    song_dropped: list[str] = []
    if song_duration_s is not None:
        song_cap = int(math.floor(float(song_duration_s) * FPS + 1e-6))
        if song_cap < floor_frames:
            raise ValueError("the song is too short to make a montage")
        if total > song_cap:
            total = _shrink(wanted, labelled, total, max(song_cap, floor_frames))
        # Still longer than the song: trim the last cut, and drop trailing clips
        # whose cut would fall under the shortest a cut can be. Dropped ids are
        # named on the receipt; nothing is silently cut.
        while total > song_cap and ordered:
            excess = total - song_cap
            if wanted[-1] - excess >= int(math.ceil(MIN_VIDEO_CUT_S * FPS)) or len(ordered) == 1:
                wanted[-1] = max(1, wanted[-1] - excess)
                total = sum(wanted)
                break
            gone = ordered.pop()
            total -= wanted.pop()
            capacity.pop()
            labelled.pop()
            growth_ceiling.pop()
            labels.pop(gone.media_id, None)
            song_dropped.append(gone.media_id)
        if total < floor_frames:
            total = _grow(wanted, capacity, floor_frames)
        short = [media_id for media_id in short if media_id not in song_dropped]
    if total / FPS > MAX_PROPOSAL_DURATION_S:
        raise ValueError("these clips make a montage that is too long")

    # ── background song: window + beat-snapped cut lengths (ms) ──────────────
    cut_ms: list[int] | None = None
    song_plan: UserSongPlan | None = None
    song_receipt: dict[str, Any] = {}
    if song_duration_s is not None:
        from app.pipeline.music_recipe import auto_best_section  # noqa: PLC0415

        nominal_ms: list[int] = []
        lo_ms: list[int] = []
        hi_ms: list[int] = []
        for clip, frames, cap_frames, ceiling in zip(
            ordered, wanted, capacity, growth_ceiling, strict=True
        ):
            tiny = clip.kind != "image" and clip.duration_s < MIN_VIDEO_CUT_S
            nominal = (
                int(math.floor(clip.duration_s * 1000)) if tiny else int(round(frames * 1000 / FPS))
            )
            label = labels.get(clip.media_id)
            floor_ms = (
                int(math.floor(label.min_display_s * 1000)) - 1 if label else _MIN_UNLABELLED_MS
            )
            nominal_ms.append(nominal)
            lo_ms.append(nominal if tiny else min(floor_ms, nominal))
            hi_ms.append(
                nominal if tiny else max(nominal, int(round(max(ceiling, frames) * 1000 / FPS)))
            )
            hi_ms[-1] = min(hi_ms[-1], max(nominal, int(round(cap_frames * 1000 / FPS))))
        # The shortest video is 3 s (``EditProposalSnapshot``): rounding every
        # cut to a millisecond must not land under it.
        deficit = int(MIN_TOTAL_S * 1000) - sum(nominal_ms)
        for index in range(len(nominal_ms) - 1, -1, -1):
            if deficit <= 0:
                break
            room = hi_ms[index] - nominal_ms[index]
            if room > 0:
                step = min(room, deficit)
                nominal_ms[index] += step
                deficit -= step
        total_ms = sum(nominal_ms)
        window_s = total_ms / 1000
        beats = sorted({float(b) for b in (song_beats or ()) if math.isfinite(float(b))})
        window_start, _window_end = auto_best_section(
            beats,
            window_s=window_s,
            track_duration_s=float(song_duration_s),
            lyric_lines=_song_line_dicts(song_lines) or None,
        )
        window_start = max(
            0.0, min(float(window_start), math.floor((song_duration_s - window_s) * 1000) / 1000)
        )
        window_start = round(window_start, 3)
        beats_ms = sorted(
            {
                int(round((beat - window_start) * 1000))
                for beat in beats
                if 0 < beat - window_start < window_s
            }
        )
        cut_ms, on_beat = _snap_boundaries_to_beats(nominal_ms, lo_ms, hi_ms, beats_ms)
        song_plan = UserSongPlan(
            mode="background",
            plan_item_id=str(song_plan_item_id),
            generation=int(song_generation),  # type: ignore[arg-type]
            duration_s=float(song_duration_s),
            window_start_s=window_start,
            window_end_s=round(window_start + total_ms / 1000, 3),
        )
        song_receipt = {
            "mode": "background",
            "window_start_s": song_plan.window_start_s,
            "window_end_s": song_plan.window_end_s,
            "beat_aligned_cuts": on_beat,
            "internal_cuts": max(0, len(cut_ms) - 1),
            "dropped_clip_ids": list(song_dropped),
        }

    cuts: list[FastMontageCut] = []
    refs: list[MediaRef] = []
    for index, (clip, frames) in enumerate(zip(ordered, wanted, strict=True)):
        duration = cut_ms[index] / 1000 if cut_ms is not None else frames / FPS
        if clip.kind == "image":
            start, end = 0.0, round(duration, 3)
        elif clip.duration_s < MIN_VIDEO_CUT_S:
            # Whole frames stop a fraction of a frame short of a 0.298s clip,
            # which the snapshot refuses: a clip this short is shown whole.
            start, end = 0.0, math.floor(clip.duration_s * 1000) / 1000
        else:
            start = _window_start_s(clip, duration)
            end = round(start + duration, 3)
            if end > clip.duration_s + 0.001:
                start = round(max(0.0, clip.duration_s - duration), 3)
                end = round(start + duration, 3)
        cuts.append(
            FastMontageCut(
                cut_id=f"unified-cut-{index + 1}",
                media_id=clip.media_id,
                source_start_s=start,
                source_end_s=end,
                output_duration_s=round(end - start, 3),
                role="hook" if index == 0 else "payoff" if index == len(ordered) - 1 else "build",
                transition="none",
                beat_align=False,
            )
        )
        aspect = clip.aspect
        if clip.width and clip.height:
            swapped = clip.orientation_degrees in (90, 270)
            aspect = (clip.height / clip.width) if swapped else (clip.width / clip.height)
        refs.append(
            MediaRef(
                lane=clip.lane,  # type: ignore[arg-type]
                media_id=clip.media_id,
                gcs_path=clip.proxy_path,
                generation=clip.generation,
                kind=clip.kind,  # type: ignore[arg-type]
                duration_s=clip.duration_s if clip.kind == "video" else None,
                aspect=aspect,
                analysis=dict(clip.analysis) if isinstance(clip.analysis, Mapping) else {},
                source_filename=clip.source_filename,
                user_context=clip.user_context,
                content_hash=clip.content_hash,
            )
        )
    total_s = round(sum(cut.output_duration_s for cut in cuts), 3)

    snapshot_kwargs: dict[str, Any] = {}
    if closing:
        snapshot_kwargs["closing_title"] = closing
    image_layout = strategy.get("image_layout")
    if image_layout in ("fullscreen", "supporting_card") and any(
        clip.kind == "image" for clip in ordered
    ):
        # The creator's own "don't crop my photos" choice; otherwise a photo
        # cut takes the fast montage's fullscreen cover crop.
        snapshot_kwargs["image_layout"] = image_layout
    hold = strategy.get("opening_title_duration_s")
    if isinstance(hold, (int, float)) and not isinstance(hold, bool):
        snapshot_kwargs["opening_title_duration_s"] = hold
    if song_plan is not None:
        snapshot_kwargs["user_song"] = song_plan
    if output_orientation in ("portrait", "landscape"):
        snapshot_kwargs["output_orientation"] = output_orientation
        snapshot_kwargs["output_orientation_reason"] = CREATOR_SELECTED_ORIENTATION_REASON
    style: dict[str, Any] = {}
    if family is not None:
        style["font_family"] = family
    if isinstance(strategy.get("text_color"), str) and strategy.get("text_color"):
        style["text_color"] = strategy["text_color"]

    def build(extra: Mapping[str, Any]) -> EditProposalSnapshot:
        return EditProposalSnapshot(
            direction="fast_montage",
            goal="",
            pace="fast",
            duration_s=total_s,
            title=(title or SNAPSHOT_FALLBACK_TITLE)[:100],
            opening_title=title,
            media=refs,
            story_beats=_story_beats(cuts),
            fast_cuts=cuts,
            media_scope="all",
            selected_media_ids=[clip.media_id for clip in ordered],
            video_reuse_policy="once",
            clip_labels=[labels[clip.media_id] for clip in ordered if clip.media_id in labels],
            **snapshot_kwargs,
            **extra,
        )

    try:
        snapshot = build(style)
    except ValidationError:
        # An unknown font or colour is a style preference, never a reason to
        # fail the render: fall back to the default typography.
        snapshot = build({})
    return UnifiedMontagePlan(
        snapshot=snapshot,
        clip_ids=[clip.media_id for clip in ordered],
        title=title,
        title_source=title_source,
        label_clip_ids=[clip.media_id for clip in ordered if clip.media_id in labels],
        label_scope_clip_ids=[
            clip.media_id for clip in ordered if clip.ref_id in (described or intent_labels)
        ],
        intent_outcomes=_intent_outcomes(strategy, clip_intents_enabled, ordered, labels),
        ordering_choice=choice,
        dropped_label_clip_ids=dropped,
        short_label_clip_ids=short,
        ordering_basis=basis,
        ordering_fallback_clip_ids=fallback_ids,
        duration_s=total_s,
        brief_version=view.version,
        wants_per_clip_text=labels_requested,
        dropped_label_reasons=dropped_reasons,
        route={key: value for key, value in (("start", start_fact), ("end", end_fact)) if value},
        # Where the first and last clips were filmed: a Visual carries no place.
        endpoint_places=_endpoint_places([clip for clip in ordered if clip.lane == "clip"]),
        visual_ids=[clip.media_id for clip in ordered if clip.lane == "asset"],
        label_timezone=hour_zone if view.per_clip_text_is_time and labels else "",
        label_timezone_basis=hour_basis if view.per_clip_text_is_time and labels else "",
        user_song=song_plan,
        song_receipt=song_receipt,
    )


def skia_font_covers(family: str, text: str) -> bool:
    """True when the bundled ``family`` has a glyph for every character of ``text``.

    The phone lays text out from exact glyph ids, so a character the font lacks
    (Fraunces has no "→") fails the whole recipe. Unknown environments (no
    skia) answer True: the compile step stays the source of truth.
    """
    try:
        import skia  # noqa: PLC0415

        from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415

        resolved = cloud._resolve_typeface_for_overlay({"font_family": family})
        glyphs = skia.Font(resolved.typeface, 60).textToGlyphs(text)
    except Exception:  # noqa: BLE001 - coverage is a best-effort pre-check
        return True
    return len(glyphs) == len(text) and all(glyph != 0 for glyph in glyphs)


_DEFAULT_FONT = "Fraunces"
_FALLBACK_FONTS = ("DM Sans",)


def _strip_uncovered(text: str, family: str, covers: Callable[[str, str], bool]) -> str:
    kept = "".join(ch for ch in text if ch.isspace() or covers(family, ch))
    return " ".join(kept.split())


def _fit_typography(
    covers: Callable[[str, str], bool] | None,
    requested: str | None,
    title: str | None,
    closing: str | None,
    labels: dict[str, ClipLabel],
) -> tuple[str | None, str | None, str | None, dict[str, ClipLabel]]:
    """Pick the first bundled font that can draw every string, keeping Unicode.

    Turkish letters are never folded to ASCII. If no candidate covers a string
    (a symbol no font has), only the uncovered characters are dropped.
    """
    if covers is None:
        return requested, title, closing, labels
    texts = [text for text in (title, closing, *(label.text for label in labels.values())) if text]
    candidates = list(dict.fromkeys(f for f in (requested, _DEFAULT_FONT, *_FALLBACK_FONTS) if f))
    chosen = next(
        (family for family in candidates if all(covers(family, text) for text in texts)), None
    )
    if chosen is None:
        chosen = candidates[0]
        title = _strip_uncovered(title, chosen, covers) or None if title else None
        closing = _strip_uncovered(closing, chosen, covers) or None if closing else None
        fixed: dict[str, ClipLabel] = {}
        for media_id, label in labels.items():
            text = _strip_uncovered(label.text, chosen, covers)
            if text:
                fixed[media_id] = label.model_copy(
                    update={"text": text, "min_display_s": min_display_s(len(text))}
                )
        labels = fixed
    keep = requested is not None or chosen != _DEFAULT_FONT
    return (chosen if keep else None), title, closing, labels


def _described_shot_labels(strategy: Mapping[str, Any], enabled: bool) -> dict[str, str]:
    """media_id -> the creator's exact words for the shot they described (KRI-296).

    Read from the server-verified ``op="caption"`` intents: the assignments say
    which clips the description matched, ``caption_text`` holds the words. Only
    the creator's own text counts; a phrase the resolver wrote is never printed.
    A caption with no ``creator_text`` is not a described shot: it names a chapter
    ("the pub") and is handled by ``_intent_labels``.
    """
    if not enabled:
        return {}
    rows: dict[str, str] = {}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not _is_resolved(intent, "caption") or not intent.get("creator_text"):
            continue
        if intent.get("caption_grounding") != "creator_text":
            continue
        text = _nfc(intent.get("caption_text"))
        if not text:
            continue
        for media_id in _members(intent):
            rows.setdefault(media_id, text)
    return rows


def _is_resolved(intent: object, op: str) -> bool:
    return (
        isinstance(intent, Mapping)
        and intent.get("op") == op
        and intent.get("status", "resolved") == "resolved"
    )


def _members(intent: Mapping[str, Any]) -> list[str]:
    return [
        str(a["media_id"])
        for a in intent.get("assignments") or []
        if isinstance(a, Mapping) and a.get("media_id")
    ]


def _group_owners(strategy: Mapping[str, Any]) -> dict[str, list[str]]:
    """media_id -> the creator's names (as written) of every group the clip is in."""
    owners: dict[str, list[str]] = {}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not _is_resolved(intent, "group"):
            continue
        name = _nfc(intent.get("attribute"))
        for media_id in _members(intent):
            if name and name not in owners.setdefault(media_id, []):
                owners[media_id].append(name)
    return owners


def _label_rows(
    strategy: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, bool]], dict[str, tuple[str, bool]]]:
    """(text rows, placeholder rows) from resolved label intents (KRI-282).

    A sport name comes from the creator's own group names, never from what the
    resolver read in a clip's record. When the creator named the groups ("football,
    dodgeball, beach volleyball ... group by sport") and the resolver's values for the
    label are those names, a clip takes the name of the ONE group it is in; a clip in
    no group, or in several, gets nothing rather than a guess like "Volleyballs".
    """
    rows: dict[str, tuple[str, bool]] = {}
    placeholders: dict[str, tuple[str, bool]] = {}
    owners = _group_owners(strategy)
    group_names = {fold_text(name) for names in owners.values() for name in names}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not _is_resolved(intent, "label"):
            continue
        assignments = [a for a in intent.get("assignments") or [] if isinstance(a, Mapping)]
        by_group = not intent.get("placeholder") and any(
            fold_text(_nfc(a.get("value"))) in group_names for a in assignments if a.get("value")
        )
        if by_group:
            for media_id, names in owners.items():
                if len(names) == 1:
                    rows.setdefault(media_id, (names[0], True))
            continue
        for assignment in assignments:
            media_id = str(assignment.get("media_id") or "")
            value = _nfc(assignment.get("value"))
            if not (media_id and value):
                continue
            grounding = assignment.get("grounding")
            if grounding == "placeholder":
                placeholders.setdefault(media_id, (value, True))
            elif media_id not in rows:
                rows[media_id] = (value, grounding == "creator_text")
    return rows, placeholders


def _intent_labels(strategy: Mapping[str, Any], enabled: bool) -> dict[str, tuple[str, bool]]:
    """media_id -> (text, creator_wrote_it) from server-verified label intents."""
    if not enabled:
        return {}
    rows, placeholders = _label_rows(strategy)
    # KRI-282: "a text for the pub" prints the chapter's own name, in the creator's
    # words, on that chapter's clips, over a broader sport tag.
    for intent in strategy.get("resolved_clip_intents") or []:
        if not _is_resolved(intent, "caption") or intent.get("creator_text"):
            continue
        text = _nfc(intent.get("caption_text"))
        if text and intent.get("caption_grounding") == "creator_text":
            for media_id in _members(intent):
                rows[media_id] = (text, True)
    # A placeholder ("Name", for the creator to replace) is the more specific
    # ask -- "individual shots of people" -- so it wins over a broader label on the
    # same clip (a sport tag). It prints as the creator's own request, never inferred.
    rows.update(placeholders)
    return rows


def _group_outcome(
    groups: list[tuple[str, list[int]]], choice: str | None = None
) -> dict[str, Any]:
    """Are the clips of every group together in the final cut, one stretch each?"""
    # A clip in several groups is ambiguous and belongs to none of them for this check.
    tally: dict[int, int] = {}
    for _name, spots in groups:
        for i in spots:
            tally[i] = tally.get(i, 0) + 1
    owned = sorted(i for i, n in tally.items() if n == 1)
    split: list[str] = []
    for name, spots in groups:
        mine = {i for i in spots if tally[i] == 1}
        stretches, inside = 0, False
        for i in owned:
            if i in mine and not inside:
                stretches += 1
            inside = i in mine
        if stretches > 1:
            split.append(f"{name} is in {stretches} stretches")
    row: dict[str, Any] = {"op": "group", "name": "group by " + ", ".join(n for n, _s in groups)}
    if split and choice == "chronological":
        # The creator was asked and chose filming order over grouping (KRI-282): not
        # a failure, but never reported as grouped either.
        return {
            **row,
            "status": "chosen",
            "reason": "you chose strictly chronological order, so " + "; ".join(split),
        }
    if split:
        return {
            **row,
            "status": "partial",
            "reason": "clips stay in the order you filmed them, so " + "; ".join(split),
        }
    if choice == "group_first":
        return {
            **row,
            "status": "met",
            "reason": "you chose grouping first, each group in the order you filmed it",
        }
    return {**row, "status": "met", "reason": None}


def _intent_outcomes(
    strategy: Mapping[str, Any],
    enabled: bool,
    ordered: Sequence[UnifiedClip],
    labels: Mapping[str, ClipLabel],
) -> list[dict[str, Any]]:
    """What each requested group / sport label / chapter text did in this plan (KRI-282).

    Computed from the finished plan, so a receipt can only report what rendered:
    ``status`` is ``met`` / ``partial`` / ``not_possible`` like a requirement receipt.
    The person placeholder is left out (its requirement receipt covers it).
    """
    if not enabled:
        return []
    sequence = _sequence_outcomes(_sequence_intents(strategy, enabled), ordered)
    position = {clip.ref_id: i for i, clip in enumerate(ordered)}
    printed = {
        clip.ref_id: labels[clip.media_id].text for clip in ordered if clip.media_id in labels
    }
    out: list[dict[str, Any]] = []
    groups: list[tuple[str, list[int]]] = []
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("placeholder"):
            continue
        op = intent.get("op")
        name = _nfc(intent.get("attribute"))
        members = [m for m in _members(intent) if m in position]
        if op == "group" and intent.get("status", "resolved") == "resolved":
            if members:
                groups.append((name, sorted(position[m] for m in members)))
            else:
                out.append(
                    {
                        "op": "group",
                        "name": name,
                        "status": "not_possible",
                        "reason": "I found no clips of it",
                    }
                )
        elif _is_resolved(intent, "caption") and not intent.get("creator_text"):
            text = _nfc(intent.get("caption_text"))
            row = {"op": "caption", "name": f"text for {name}"}
            if not members:
                out.append({**row, "status": "not_possible", "reason": "I found no clips of it"})
            elif not text or not any(printed.get(m) == text for m in members):
                reason = "I couldn't put your own words on it"
                out.append({**row, "status": "not_possible", "reason": reason})
            else:
                out.append({**row, "status": "met", "reason": None})
    out.extend(sequence)
    if groups:
        out.append(_group_outcome(groups, _ordering_choice(strategy, enabled)))
        names = {fold_text(n) for n, _spots in groups}
        for intent in strategy.get("resolved_clip_intents") or []:
            if not _is_resolved(intent, "label") or intent.get("placeholder"):
                continue
            row = {"op": "label", "name": f"the {_nfc(intent.get('attribute'))} name on its clips"}
            if any(fold_text(t) in names for t in printed.values()):
                out.append({**row, "status": "met", "reason": None})
            else:
                out.append({**row, "status": "not_possible", "reason": "no clip got one"})
    return out


def _title(strategy: Mapping[str, Any], view: BriefView) -> tuple[str | None, str]:
    confirmed = _nfc(strategy.get("opening_title"))
    if confirmed:
        return confirmed[:280], "creator"
    if view.title_literal:
        return _nfc(view.title_literal)[:280], "creator"
    start = _first(view.facts, _START_KEYS)
    end = _first(view.facts, _END_KEYS)
    if view.global_literal:
        route = f"{start} → {end}" if start and end else ""
        headline = _nfc(view.global_literal)
        if route and route.casefold() not in headline.casefold():
            headline = f"{headline} · {route}"
        return headline[:100], "brief"
    generated = title_from_facts(view.facts)
    if generated:
        return generated, "brief"
    # The strict snapshot still needs an internal title, but creator-visible
    # opening text does not. Never burn a generic label, a model-authored hook,
    # or unrequested place text taken from clip facts.
    return None, "none"


__all__ = [
    "BriefView",
    "UnifiedClip",
    "UnifiedMontagePlan",
    "brief_view",
    "min_display_s",
    "plan_unified_montage",
    "selected_visual_ids",
    "skia_font_covers",
    "title_from_facts",
]
