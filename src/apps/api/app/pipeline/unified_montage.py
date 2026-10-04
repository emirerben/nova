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
    # Zone the filming hours were printed in, "" when the labels are not hours.
    label_timezone: str = ""
    label_timezone_basis: str = ""

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
        zone = (
            {
                "label_timezone": self.label_timezone,
                "label_timezone_basis": self.label_timezone_basis,
            }
            if self.label_timezone
            else {}
        )
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
            **zone,
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


def plan_unified_montage(
    clips: Sequence[UnifiedClip],
    view: BriefView | None = None,
    *,
    strategy: Mapping[str, Any] | None = None,
    clip_intents_enabled: bool = False,
    font_covers: Callable[[str, str], bool] | None = None,
    creator_order: Sequence[int] = (),
    visuals: Sequence[UnifiedClip] = (),
    output_orientation: str | None = None,
) -> UnifiedMontagePlan:
    """Build the guided fast-montage plan for ``clips`` (attachment order).

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
    if not clips:
        raise ValueError("a montage needs at least one clip")
    if any(visual.lane != "asset" for visual in visuals):
        raise ValueError("montage visuals must come from the Visuals pool")
    if len({clip.media_id for clip in (*clips, *visuals)}) != len(clips) + len(visuals):
        raise ValueError("montage clips must have unique media identities")
    ordered, basis, fallback_ids = _order(clips, view, creator_order)
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
        elif view.wants_per_clip_text:
            fact = _fact_label(clip)
            if fact is not None:
                chosen = (fact[0], "creator" if fact[1] == "creator" else "fact", fact[1], fact[2])
            elif index == 0 and start_fact:
                chosen = (start_fact[:MAX_LABEL_CHARS], "brief", "start", False)
            elif index == len(ordered) - 1 and end_fact and len(ordered) > 1:
                chosen = (end_fact[:MAX_LABEL_CHARS], "brief", "end", False)
        if chosen is None:
            if labels_requested and not described:
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
    if total / FPS > MAX_PROPOSAL_DURATION_S:
        raise ValueError("these clips make a montage that is too long")

    cuts: list[FastMontageCut] = []
    refs: list[MediaRef] = []
    for index, (clip, frames) in enumerate(zip(ordered, wanted, strict=True)):
        duration = frames / FPS
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
        label_scope_clip_ids=[clip.media_id for clip in ordered if clip.ref_id in described],
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
    """
    if not enabled:
        return {}
    rows: dict[str, str] = {}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "caption":
            continue
        if intent.get("status", "resolved") != "resolved":
            continue
        if intent.get("caption_grounding") != "creator_text":
            continue
        text = _nfc(intent.get("caption_text"))
        if not text:
            continue
        for assignment in intent.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            media_id = str(assignment.get("media_id") or "")
            if media_id:
                rows.setdefault(media_id, text)
    return rows


def _intent_labels(strategy: Mapping[str, Any], enabled: bool) -> dict[str, tuple[str, bool]]:
    """media_id -> (text, creator_wrote_it) from server-verified label intents."""
    if not enabled:
        return {}
    rows: dict[str, tuple[str, bool]] = {}
    # KRI-282: a placeholder ("Name", for the creator to replace) is the more specific
    # ask -- "individual shots of people" -- so it wins over a broader label on the
    # same clip (a sport tag). It prints as the creator's own request, never inferred.
    placeholders: dict[str, tuple[str, bool]] = {}
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "label":
            continue
        for assignment in intent.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            media_id = str(assignment.get("media_id") or "")
            value = _nfc(assignment.get("value"))
            if not (media_id and value):
                continue
            grounding = assignment.get("grounding")
            if grounding == "placeholder":
                placeholders.setdefault(media_id, (value, True))
            elif media_id not in rows:
                rows[media_id] = (value, grounding == "creator_text")
    rows.update(placeholders)
    return rows


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
