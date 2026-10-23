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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.kria.brief_route import (
    END_KEYS,
    START_KEYS,
    chapter_list,
    first_text,
    fold_text,
    loose_text,
    wants_filming_time_text,
    wants_hour_only_text,
)
from app.kria.freeze_ask import FreezeSpec, freeze_spec, times_one_moment
from app.pipeline.best_parts import BestPart, BestPartsResult, select_best_parts
from app.pipeline.pinned_text import drawable_pins, pins_from_strategy
from app.schemas.clip_intents import PLACEHOLDER_LABEL_TEXT
from app.schemas.edit_proposal import (
    CREATOR_SELECTED_ORIENTATION_REASON,
    MAX_PROPOSAL_DURATION_S,
    MIN_PROPOSAL_DURATION_S,
    ClipLabel,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    PinnedText,
    StoryBeat,
    canonical_media_digest,
    mixed_media_hold_bounds,
)
from app.schemas.text_style_intent import (
    normalize_label_position,
    normalize_title_animation,
)
from app.schemas.user_song import UserSongPlan
from app.services.clip_facts import (
    CAPTURE_ORDER_KEYS,
    capture_time_from_facts,
    display_timezone,
    format_capture_hour,
    order_by_capture_time,
)
from app.services.clip_order_sequence import (
    MomentSeats,
    apply_sequence,
    moment_seats,
    row_relations,
    sequence_rows,
)
from app.services.length_policy import natural_length_owner
from app.services.spoken_quotes import is_outro_hallucination
from app.services.spoken_roles import (
    SpokenClip,
    SpokenSeats,
    speech_ordered,
    spoken_seats,
    window_holds,
    window_inside,
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
MIN_TOTAL_S = float(MIN_PROPOSAL_DURATION_S)
# Model- and fact-derived labels stay short; the creator's own words are never cut
# (``ClipLabel`` allows 120).
MAX_LABEL_CHARS = 60
MAX_CREATOR_LABEL_CHARS = 120
# ``EditProposalSnapshot.title`` is required even when no creator-visible title
# exists. Keep its schema placeholder separate from ``opening_title`` so this
# internal label never becomes an on-screen text layer (KRI-255).
SNAPSHOT_FALLBACK_TITLE = "Montage"
_CAPTURE_ORDER_KEYS = CAPTURE_ORDER_KEYS  # shared with the render contract and receipts
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
    if provenance == "creator" and len(text) > limit:
        raise ValueError("Confirmed clip text exceeds the supported label length")
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
    # KRI-585: an order requirement says "the order I added them", so what the clips
    # say never moves one (filming-time keys set ``order_by_capture`` instead).
    order_basis_stated: bool = False
    # KRI-219: the per-clip text is the hour each clip was filmed, not its place.
    per_clip_text_is_time: bool = False
    # "hh_mm" or "hour" (just the hour, no minutes) for those filming-time labels.
    time_format: str = "hh_mm"
    title_literal: str | None = None
    global_literal: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    target_duration_s: float | None = None
    # KRI-522: how the creator asked the title to enter / where labels sit / that the
    # labels are placeholders to fill in later. Typed here so they survive turns.
    title_animation: str | None = None
    label_position: str | None = None
    placeholder_labels: bool = False
    # KRI-828: the creator's freeze ask (corroborated by their words), if any.
    freeze: FreezeSpec | None = None


def brief_view(brief: Any) -> BriefView:
    """Reduce a ``CreativeBrief`` (duck-typed, see ``app.kria.brief``) to a view."""
    if brief is None:
        return BriefView()
    wants_text = False
    wants_order = False
    order_capture = False
    basis_stated = False
    wants_time = False
    hour_only = False
    clip_literals: dict[str, str] = {}
    title_literal: str | None = None
    global_literal: str | None = None
    facts: dict[str, Any] = {}
    target: float | None = None
    title_animation: str | None = None
    label_position: str | None = None
    placeholder_labels = False
    freeze: FreezeSpec | None = None
    for req in brief.live():
        if freeze is None:
            freeze = freeze_spec(req)
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
            if req.scope == "title":
                # The animation is a title ask even before the creator words the hook.
                title_animation = title_animation or normalize_title_animation(
                    (req.facts or {}).get("animation")
                )
            if req.scope == "per_clip":
                wants_text = True
                label_position = label_position or normalize_label_position(
                    (req.facts or {}).get("position")
                )
                placeholder_labels = placeholder_labels or _fact_is_true(
                    (req.facts or {}).get("placeholder")
                )
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
            from app.services.choice_questions import ATTACHMENT_ORDER_KEY  # noqa: PLC0415

            # A "narrative" or a first/last-clip key is not a basis: it is what the
            # creator's own sequence rows (and the clips' words, KRI-585) decide.
            basis_stated = basis_stated or key.casefold() == ATTACHMENT_ORDER_KEY
        elif req.kind == "timing" and not times_one_moment(req):
            # KRI-777: "freeze the frame for one second on the bite" is not a one-second video.
            raw = (req.facts or {}).get("duration_s")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
                target = float(raw)
    return BriefView(
        version=int(getattr(brief, "version", 0) or 0) or None,
        wants_per_clip_text=wants_text,
        clip_literals=clip_literals,
        wants_order=wants_order,
        order_by_capture=order_capture,
        order_basis_stated=basis_stated,
        per_clip_text_is_time=wants_time,
        time_format="hour" if hour_only else "hh_mm",
        title_literal=title_literal,
        global_literal=global_literal,
        facts=facts,
        target_duration_s=target,
        title_animation=title_animation,
        label_position=label_position,
        placeholder_labels=placeholder_labels,
        freeze=freeze,
    )


def _fact_is_true(value: object) -> bool:
    """A brief fact the extractor wrote as ``true`` (or the string "true"/"yes")."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1"}


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
    # KRI-517: the closing clip's whole spoken line, held because the creator placed
    # that clip last with its own audio kept ({media_id, source_start_s, source_end_s}).
    # Empty when no line was held, so every earlier record is unchanged.
    closing_speech: dict[str, Any] = field(default_factory=dict)
    # KRI-597: the line the creator quoted to end on, as the approved length plan found
    # it ({quote, status, media_id}); "found" lines also fill ``closing_speech`` (with
    # ``quote``) when the closing cut holds them. Empty without such an ask.
    closing_quote: dict[str, Any] = field(default_factory=dict)
    # KRI-496: exact footage windows chosen by the deterministic selector. This
    # lives in the receipt rather than the strict snapshot schema so existing
    # stored proposal payloads stay wire-compatible.
    best_parts_receipt: list[dict[str, Any]] = field(default_factory=list)
    # KRI-624: each footage clip's own length, so a receipt can tell the long takes apart.
    clip_durations_s: dict[str, float] = field(default_factory=dict)
    best_parts_conflicts: list[dict[str, Any]] = field(default_factory=list)
    # KRI-569: the song's lyrics placed as captions ({status, style, words, lines}). Empty
    # when no lyric captions were planned, so every earlier record is unchanged.
    caption_receipt: dict[str, Any] = field(default_factory=dict)
    # KRI-585: media_id -> spoken role ("opening" / "closing" / "introduction") of the
    # clips the spoken order read; the welcome it moved forward, when its cut holds the
    # line ({media_id, source_start_s, source_end_s}); and the creator-pinned closing
    # clip cut on a stretch without its introduction (the same plus ``reason``). All
    # empty unless the spoken order ran and found a role, so every earlier record is
    # unchanged.
    spoken_roles: dict[str, str] = field(default_factory=dict)
    opening_speech: dict[str, Any] = field(default_factory=dict)
    closing_quiet: dict[str, Any] = field(default_factory=dict)
    # KRI-760: the lines the creator asked to hear whole, one row per quote: {quote, status
    # ("held" / "not_held" / "ambiguous" / "not_found"), media_id, source_start_s,
    # source_end_s}. Empty without such an ask, so every earlier record is unchanged.
    named_lines: list[dict[str, Any]] = field(default_factory=list)
    # KRI-828: the creator's freeze as planned: {requirement_id, status ("held" /
    # "not_grounded"), media_id, cut_id, frame_source_s, hold_s, grounding}. Empty without
    # a freeze ask (or when the route cannot draw one), so every earlier record is unchanged.
    freeze_frame: dict[str, Any] = field(default_factory=dict)

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
        speech = {"closing_speech": dict(self.closing_speech)} if self.closing_speech else {}
        quote = {"closing_quote": dict(self.closing_quote)} if self.closing_quote else {}
        named = {"named_lines": [dict(row) for row in self.named_lines]} if self.named_lines else {}
        freeze = {"freeze_frame": dict(self.freeze_frame)} if self.freeze_frame else {}
        best_parts = (
            {"best_parts": list(self.best_parts_receipt)} if self.best_parts_receipt else {}
        )
        # Only a plan that picked source windows needs clip lengths to be judged (KRI-624).
        durations = (
            {"clip_durations_s": dict(self.clip_durations_s)}
            if self.best_parts_receipt and self.clip_durations_s
            else {}
        )
        best_parts_conflicts = (
            {"best_parts_conflicts": list(self.best_parts_conflicts)}
            if self.best_parts_conflicts
            else {}
        )
        captions = {"captions": dict(self.caption_receipt)} if self.caption_receipt else {}
        spoken = {
            key: dict(value)
            for key, value in (
                ("spoken_roles", self.spoken_roles),
                ("opening_speech", self.opening_speech),
                ("closing_quiet", self.closing_quiet),
            )
            if value
        }
        pins = (
            {"pinned_texts": [pin.model_dump(mode="json") for pin in self.snapshot.pinned_texts]}
            if self.snapshot.pinned_texts
            else {}
        )
        record = {
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
                    **({"group_id": label.group_id} if label.group_id else {}),
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
            **pins,
            **scope,
            **outcomes,
            **choice,
            **zone,
            **named,
            **freeze,
            **song,
            **speech,
            **quote,
            **best_parts,
            **durations,
            **best_parts_conflicts,
            **captions,
            **spoken,
        }

        if self.snapshot.text_composition is not None:
            from app.pipeline.guided_story import (
                compile_proposal_execution_plan,
                group_title_prefix,
            )

            texts = compile_proposal_execution_plan(self.snapshot)["text_elements"]

            def wording(source_id: str) -> str | None:
                rows = [
                    row
                    for row in texts
                    if row["id"] == source_id
                    or (row.get("source_params") or {}).get("sequence_source_id") == source_id
                ]
                return (
                    " ".join(row["text"] for row in sorted(rows, key=lambda row: row["start_s"]))
                    or None
                )

            record["title"] = wording("guided-title")
            actual_labels = []
            for label in record["labels"]:
                cut = next(
                    (
                        cut
                        for cut in self.snapshot.fast_cuts or []
                        if cut.media_id == label["media_id"]
                    ),
                    None,
                )
                text = wording(f"clip-label-{cut.cut_id}") if cut else None
                if text is None and label.get("group_id"):
                    # One continuous title covers the whole stretch: read it back from
                    # the first group-title element of the chapter.
                    prefix = group_title_prefix(label["group_id"])
                    first = next(
                        (row["id"] for row in texts if str(row["id"]).startswith(prefix)), None
                    )
                    text = wording(first) if first else None
                if text is not None:
                    actual_labels.append({**label, "text": text})
            record["labels"] = actual_labels
        return record

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


# The shortest an unlabelled cut may be shrunk to: the readable-shot floor. Public so
# the clarification gate (``choice_questions.collect_conflicts``) asks about exactly
# the length the planner can never go below, never about a number of its own.
MIN_READABLE_SHOT_S = 0.8
_MIN_UNLABELLED_FRAMES = int(round(MIN_READABLE_SHOT_S * FPS))


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


def _frame_bound_best_parts(parts: BestPartsResult) -> BestPartsResult:
    """Keep selected windows source-bounded and whole-frame for phone/cloud parity."""
    bounded = []
    for part in parts.parts:
        if part.reason == "analysis_fallback" and part.duration_s < MIN_VIDEO_CUT_S:
            bounded.append(part)
            continue
        start = math.ceil(part.source_start_s * FPS - 1e-9) / FPS
        end = math.floor(part.source_end_s * FPS + 1e-9) / FPS
        # A 100ms included-source floor is exactly three 30fps frames. Preserve
        # the original validated window when rounding cannot keep that floor.
        if end - start < 0.1 - 1e-9:
            bounded.append(part)
        else:
            bounded.append(replace(part, source_start_s=start, source_end_s=end))
    return replace(
        parts, parts=tuple(bounded), duration_s=round(sum(p.duration_s for p in bounded), 3)
    )


# KRI-517: "end on Elif's sentence, in her own voice". Analysed speech bounds are
# approximate (Gemini segment times), so the held window keeps a breath either side.
# Measured on the M3 kit's closing clip: speech 0.1-5.45 s by loudness, analysed 0.00-5.44 s.
SPEECH_LEAD_S = 0.15
SPEECH_TAIL_S = 0.3
# A held line counts as held when the cut covers it to within a frame or two.
_HELD_LINE_TOLERANCE_S = 0.05
# KRI-585: above every analysed moment's energy, so a seated welcome line or quiet
# closing stretch is its clip's window (the length plan's ``DESCRIBED_MOMENT_ENERGY``).
SPOKEN_SEAT_ENERGY = 100.0


def _speech_span(clip: UnifiedClip) -> tuple[float, float] | None:
    """The clip's analysed speech, first word to last, padded and kept inside the clip."""
    analysis = clip.analysis if isinstance(clip.analysis, Mapping) else {}
    understanding = analysis.get("understanding")
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    if not isinstance(speech, Mapping) or speech.get("has_speech") is not True:
        return None
    bounds: list[tuple[float, float]] = []
    for segment in speech.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        if is_outro_hallucination(str(segment.get("text") or ""), analysis):
            # whisper's "thanks for watching" on trailing wind would hold the cut open.
            continue
        try:
            start, end = float(segment.get("start_s")), float(segment.get("end_s"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            bounds.append((start, end))
    if not bounds:
        return None
    start = max(0.0, min(s for s, _e in bounds) - SPEECH_LEAD_S)
    end = min(float(clip.duration_s), max(e for _s, e in bounds) + SPEECH_TAIL_S)
    if end - start < MIN_VIDEO_CUT_S:
        return None
    return round(start, 3), round(end, 3)


def _keeps_source_audio(strategy: Mapping[str, Any]) -> bool:
    audio = strategy.get("montage_audio")
    if isinstance(audio, Mapping) and audio.get("preserve_source_audio") is True:
        return True
    return strategy.get("audio_strategy") == "original_audio"


def _closing_speech_span(
    ordered: Sequence[UnifiedClip],
    rows: Sequence[tuple[str | None, str, list[str], str]],
    strategy: Mapping[str, Any],
) -> tuple[float, float] | None:
    """The spoken line the closing cut must hold whole, if the creator asked to end on it.

    Only when the creator placed the closing clip last (a resolved ``last`` sequence
    row names it), the clip speaks, and the camera audio is kept: a montage that just
    happens to end on a talking clip keeps its normal cut.
    """
    if not ordered or not _keeps_source_audio(strategy):
        return None
    closing = ordered[-1]
    if closing.lane != "clip" or closing.kind != "video":
        return None
    placed_last = any(
        position == "last" and status == "resolved" and closing.ref_id in members
        for position, _name, members, status in rows
    )
    return _speech_span(closing) if placed_last else None


def _source_second(value: object) -> float | None:
    """A source time in seconds (0 included), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _closing_records(
    cuts: Sequence[FastMontageCut],
    speech_span: tuple[float, float] | None,
    quote: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(closing_speech, closing_quote) records, claimed only when the last cut holds them.

    KRI-517's whole spoken line is recorded when the closing cut covers it; KRI-597's
    quoted line (found by the approved length plan) when the closing cut is that clip
    and covers the line. An approved window that does not hold the line records
    nothing, so no receipt can call a missing line "held".
    """
    if not cuts:
        return {}, {}
    last = cuts[-1]

    def holds(media_id: object, start: float, end: float) -> bool:
        return (
            last.media_id == media_id
            and last.source_start_s <= start + _HELD_LINE_TOLERANCE_S
            and last.source_end_s >= end - _HELD_LINE_TOLERANCE_S
        )

    if isinstance(quote, Mapping) and quote.get("quote"):
        record = {
            key: quote[key] for key in ("quote", "status", "media_id", "intent_id") if key in quote
        }
        start, end = _source_second(quote.get("start_s")), _source_second(quote.get("end_s"))
        if quote.get("status") != "found" or start is None or end is None:
            return {}, record
        if not holds(quote.get("media_id"), start, end):
            return {}, {**record, "status": "not_held"}
        speech = {
            "media_id": last.media_id,
            "source_start_s": start,
            "source_end_s": end,
            "quote": str(quote["quote"]),
        }
        return speech, record
    if speech_span is not None and holds(last.media_id, *speech_span):
        return {
            "media_id": last.media_id,
            "source_start_s": last.source_start_s,
            "source_end_s": last.source_end_s,
        }, {}
    return {}, {}


def _named_line_records(cuts: Sequence[FastMontageCut], planned: object) -> list[dict[str, Any]]:
    """The approved plan's whole-line rows (KRI-760), "held" only when a cut covers the line.

    A line the approved plan found is "held" when some cut of its clip covers the sentence's
    seconds and "not_held" when none does (the clip was left out, or its cut is shorter than
    the line); a line it could not place stays as it was.
    """
    out: list[dict[str, Any]] = []
    for row in planned if isinstance(planned, list) else []:
        if not isinstance(row, Mapping) or not row.get("quote") or not row.get("status"):
            continue
        record: dict[str, Any] = {"quote": str(row["quote"]), "status": str(row["status"])}
        start, end = _source_second(row.get("start_s")), _source_second(row.get("end_s"))
        if record["status"] == "found" and start is not None and end is not None and end > start:
            record.update(media_id=str(row.get("media_id") or ""))
            record.update(source_start_s=start, source_end_s=end)
            held = any(
                cut.media_id == record["media_id"]
                and cut.source_start_s <= start + _HELD_LINE_TOLERANCE_S
                and cut.source_end_s >= end - _HELD_LINE_TOLERANCE_S
                for cut in cuts
            )
            record["status"] = "held" if held else "not_held"
        elif record["status"] == "found":
            continue
        out.append(record)
    return out


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
    """The creator's stated sequence (KRI-458); the rule lives in ``clip_order_sequence``
    so the render contract pins the exact order this planner lays out (KRI-503)."""
    if not enabled:
        return []
    return sequence_rows(strategy.get("resolved_clip_intents"))


def _apply_sequence(
    ordered: Sequence[UnifiedClip], rows: Sequence[tuple[str | None, str, list[str], str]]
) -> list[UnifiedClip]:
    """Seat the described groups: ``first`` ones lead, ``last`` ones close, the rest follow."""
    return apply_sequence(ordered, rows, key=lambda clip: clip.ref_id)


def _sequence_outcomes(
    rows: Sequence[tuple[str | None, str, list[str], str]],
    ordered: Sequence[UnifiedClip],
    *,
    seats: MomentSeats | None = None,
    cuts: Sequence[tuple[str, float]] = (),
) -> list[dict[str, Any]]:
    """Did each described group land where the creator said? Read off the finished order.

    Every row also carries a ``code`` (KRI-503) so the receipt never re-parses ``reason``:
    ``landed`` / ``misplaced`` (a described group is not where it was asked), ``absent`` (none
    of its clips are in the edit), ``contained`` (a ``then`` group whose clips are all already
    in an earlier group), ``repeat`` (a first / last group naming clips an earlier group
    already seated at that same spot: the creator said it twice, KRI-764), ``conflict`` (a
    first / last group naming clips an earlier group seated at another spot: one clip cannot
    be both, the earlier claim stands), ``visual`` (its members are Visuals-pool items, which
    no sequence rule seats) and ``unresolved``. ``status`` keeps its meaning for every other
    consumer.

    ``seats`` (KRI-796) are the rows read as moments of one take (``moment_seats``) and
    ``cuts`` the finished cut order as (clip ref, source start): those rows are judged off
    the cuts by ``_moment_outcomes`` and carry the ``moment`` they name or follow.
    """
    spot = {clip.ref_id: i for i, clip in enumerate(c for c in ordered if c.lane == "clip")}
    visuals = {clip.ref_id for clip in ordered if clip.lane != "clip"}
    named = {m for _p, _n, members, _s in rows for m in members if m in spot}
    relations = row_relations(rows, present=spot)
    moments = _moment_outcomes(seats, cuts) if seats is not None else {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    landed_clips: set[str] = set()
    for index, ((position, name, members, status), relation) in enumerate(
        zip(rows, relations, strict=True)
    ):
        label = f"{position or 'then'}: {name}" if name else "the order you described"
        row = {"op": "order", "name": label, "position": position or "then"}
        if index in moments:
            seen.update(m for m in members if m in spot)
            out.append({**row, **moments[index]})
            continue
        mine = [m for m in members if m in spot and m not in seen]
        if status != "resolved":
            out.append(
                {
                    **row,
                    "status": "not_possible",
                    "code": "unresolved",
                    "reason": "I couldn't tell which clips",
                }
            )
            continue
        if relation == "repeat":
            # The same ask twice: it stands or falls with the group that seated those clips.
            met = all(m in landed_clips for m in members if m in spot)
            out.append(
                {
                    **row,
                    "status": "met" if met else "partial",
                    "code": "repeat",
                    "reason": None if met else "the clips did not end up there",
                }
            )
            continue
        if relation == "conflict":
            out.append(
                {
                    **row,
                    "status": "partial",
                    "code": "conflict",
                    "reason": "its clips are already placed at another spot",
                }
            )
            continue
        if not mine:
            if any(m in seen for m in members):
                code, reason = "contained", "its clips are already in an earlier group"
            elif members and all(m in visuals for m in members):
                code, reason = "visual", "I found no clips of it"
            else:
                code, reason = "absent", "I found no clips of it"
            out.append({**row, "status": "not_possible", "code": code, "reason": reason})
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
        if met:
            landed_clips.update(mine)
        out.append(
            {
                **row,
                "status": "met" if met else "partial",
                "code": "landed" if met else "misplaced",
                "reason": None if met else "the clips did not end up there",
            }
        )
    return out


def _moment_outcomes(
    seats: MomentSeats, cuts: Sequence[tuple[str, float]]
) -> dict[int, dict[str, Any]]:
    """KRI-796: each row ``moment_seats`` read, judged off the finished cuts.

    A moment's row is met when the take plays as exactly its windows, in take order (the
    i-th cut of the take is the i-th moment). A Visual's row is met when every Visual it
    names plays right after its moment: as many cuts of the take before it as the moment's
    number. Never read off the seating itself, so a layout that misses the spot says so.
    """
    starts = [start for ref, start in cuts if ref == seats.take]
    in_order = len(starts) == seats.moments and starts == sorted(starts)
    out: dict[int, dict[str, Any]] = {}
    for index, moment in seats.named.items():
        out[index] = {
            "status": "met" if in_order else "partial",
            "code": "landed" if in_order else "misplaced",
            "reason": None if in_order else "the clips did not end up there",
            "moment": moment,
        }
    for index, (moment, members) in seats.placed.items():
        spots = [next((i for i, (ref, _s) in enumerate(cuts) if ref == m), None) for m in members]
        if None in spots:
            out[index] = {
                "status": "not_possible",
                "code": "absent",
                "reason": "I found no clips of it",
                "moment": moment,
            }
            continue
        met = in_order and all(
            sum(ref == seats.take for ref, _s in cuts[:spot]) == moment for spot in spots
        )
        out[index] = {
            "status": "met" if met else "partial",
            "code": "landed" if met else "misplaced",
            "reason": None if met else "the clips did not end up there",
            "moment": moment,
        }
    return out


_Slot = tuple[int, UnifiedClip, int, BestPart | None]


def _seat_among_moments(slots: list[_Slot], seats: MomentSeats) -> list[_Slot]:
    """KRI-796: a seated Visual's cut plays right after its moment, the take's n-th window.

    ``slots`` are the cuts to emit as (index into ``ordered``, clip, frames, window), in
    the order the clips were laid out; every other cut keeps its place.
    """
    moved = [slot for slot in slots if slot[1].ref_id in seats.after]
    out: list[_Slot] = []
    played = 0
    for slot in slots:
        if slot[1].ref_id in seats.after:
            continue
        out.append(slot)
        if slot[1].ref_id == seats.take:
            played += 1
            out.extend(m for m in moved if seats.after[m[1].ref_id] == played)
    return out if len(out) == len(slots) else slots


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
    creator_duration_proven: bool = False,
    approved_length_plan: Mapping[str, Any] | None = None,
    speech_order: bool = False,
    freeze_frames: bool = False,
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

    ``speech_order`` (KRI-585) says every clip's own sound plays in this montage and no
    confirmed order is pinned for it. Then, in an order nobody chose (attachment order,
    no filming- or upload-order ask, no grouping answer, no song), a clip whose words
    welcome the viewer leads and a sign-off closes (``services.spoken_roles``), with the
    creator's own sequence rows seated on top. Without approved windows the welcome's line
    is held whole and a creator-pinned closing clip that only introduces someone ends on
    a quiet stretch of that clip; an approved length plan already chose those windows.
    Off (the default), or with no spoken role in any clip, the plan is byte-identical.

    ``freeze_frames`` (KRI-828) says the renderer that will draw this plan can hold a frame
    (``freezeFrame`` verified). Then the creator's freeze ask (``view.freeze``) is grounded
    to one cut (see ``_ground_freeze``): that cut's window ends on the frozen frame and holds
    it. Off, or with no freeze ask, the plan is byte-identical.
    """
    view = view or BriefView()
    strategy = strategy or {}
    if (
        view.version is not None
        and view.title_literal
        and not view.wants_per_clip_text
        and not view.global_literal
        and chapter_list(
            view.title_literal,
            [value[0] for value in _intent_labels(strategy, clip_intents_enabled).values()]
            + list(_described_shot_labels(strategy, clip_intents_enabled).values()),
        )
        is None
    ):
        # A hook's explanatory context is not a second caption. A bound brief
        # owns whether clip text was requested; preserve every non-text intent.
        strategy = {
            **strategy,
            "shot_labels": None,
            "resolved_clip_intents": [
                row
                for row in strategy.get("resolved_clip_intents") or []
                if not isinstance(row, Mapping) or row.get("op") not in {"caption", "label"}
            ],
        }
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
    # KRI-585: the viewer hears every clip's own sound, so a welcome leads and a
    # sign-off closes. Only an order nobody chose moves (see ``speech_order``).
    seats = SpokenSeats()
    if (
        speech_order
        and basis == "attachment"
        and not view.order_by_capture
        and not view.order_basis_stated
        and choice is None
        and song_duration_s is None
    ):
        quote = (approved_length_plan or {}).get("closing_quote")
        seats = spoken_seats(
            [
                SpokenClip(clip.ref_id, clip.analysis, float(clip.duration_s))
                for clip in ordered
                if clip.lane == "clip" and clip.kind == "video"
            ],
            sequence,
            hold_closing_words=_keeps_source_audio(strategy)
            or (isinstance(quote, Mapping) and quote.get("status") == "found"),
        )
        ordered = speech_ordered(ordered, seats.roles, key=lambda clip: clip.ref_id)
    if sequence:
        ordered = _apply_sequence(ordered, sequence)
    ordered = _scatter(ordered, visuals)
    editorial = strategy.get("editorial_plan")
    if editorial is not None:
        from app.services.editorial_plan import editorial_order

        existing = [clip.media_id for clip in ordered]
        ids = editorial_order(
            editorial,
            existing,
            required_order=existing if view.order_by_capture or creator_order else (),
            sequence=sequence,
        )
        by_id = {clip.media_id: clip for clip in ordered}
        ordered = [by_id[media_id] for media_id in ids]
        basis = "approved_editorial_plan"

    if view.order_by_capture:
        # Visuals carry no capture time: they keep their spread slot, and the
        # receipt says their place is not the filmed order.
        fallback_ids = [*fallback_ids, *(v.media_id for v in visuals if v.capture_time is None)]

    # KRI-496: only footage-owned, unnamed timing gets its length and source
    # windows from analysis. Song, voiceover, original-audio takes and a proven
    # creator duration keep their dedicated policies below. Images retain the
    # established Visuals hold behavior because they have no source window.
    best_parts: BestPartsResult | None = None
    best_parts_conflicts: list[dict[str, Any]] = []
    has_approved_windows = False
    mixed_media_timing: MixedMediaTimingProfile | None = None
    raw_mixed_media_timing = strategy.get("mixed_media_timing")
    if isinstance(raw_mixed_media_timing, MixedMediaTimingProfile):
        mixed_media_timing = raw_mixed_media_timing
    elif isinstance(raw_mixed_media_timing, Mapping):
        try:
            mixed_media_timing = MixedMediaTimingProfile.model_validate(raw_mixed_media_timing)
        except ValidationError:
            # Strategy is model/client supplied.  Invalid timing cannot become
            # an implicit renderer policy.
            mixed_media_timing = None

    # An approved proposal envelope is the one authoritative selection of
    # source windows.  It is deliberately validated before any local selector
    # can run: stale or conflicted envelopes must block rather than quietly
    # falling back to legacy whole-clip windows.
    if approved_length_plan is not None:
        from app.services.length_planning import read_length_plan  # noqa: PLC0415

        conflicts = approved_length_plan.get("conflicts")
        if not isinstance(conflicts, list):
            raise ValueError("invalid approved length plan conflicts")
        if conflicts:
            raise ValueError("approved length plan has unresolved conflicts")
        approved_parts = read_length_plan(approved_length_plan, ordered)
        if approved_parts is None:
            raise ValueError("invalid approved length plan")
        if approved_parts:
            has_approved_windows = True
            best_parts = BestPartsResult(
                approved_parts,
                round(sum(part.duration_s for part in approved_parts), 3),
                round(sum(part.duration_s for part in approved_parts), 3),
                round(sum(part.duration_s for part in approved_parts), 3),
                (),
            )

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
    chapter_titles, described_titles = _chapter_group_ids(strategy, clip_intents_enabled)
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
        group_id: str | None = None
        if clip.ref_id in view.clip_literals:
            chosen = (view.clip_literals[clip.ref_id], "creator", None, False)
        elif clip.ref_id in described:
            chosen = (described[clip.ref_id], "creator", None, False)
            shared = described_titles.get(clip.ref_id)
            if shared is not None and shared[1] == described[clip.ref_id]:
                group_id = shared[0]
        elif not described and index < len(positional):
            chosen = (positional[index], "creator", None, False)
        elif (
            view.placeholder_labels
            and view.wants_per_clip_text
            and not view.per_clip_text_is_time
            and not described
        ):
            # KRI-522: "placeholder location on each video" is the creator's own ask and
            # stays in force across turns (the brief). Every clip prints the fixed
            # placeholder, never a place read from footage in its stead.
            chosen = (PLACEHOLDER_LABEL_TEXT, "creator", None, False)
        elif clip.ref_id in intent_labels:
            text, creator_text = intent_labels[clip.ref_id]
            chosen = (text, "creator" if creator_text else "fact", None, not creator_text)
            # A chapter title shared with the neighbouring clips of its chapter prints
            # once across them; a sport tag or placeholder that won the clip does not.
            chapter = chapter_titles.get(clip.ref_id)
            if chapter is not None and chapter[1] == text:
                group_id = chapter[0]
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
            group_id=group_id,
        )

    # ── title and typography ─────────────────────────────────────────────────
    title, title_source = _title(strategy, view, [label.text for label in labels.values()])
    closing = _nfc(strategy.get("closing_title")) or None
    pins = _pinned_texts(strategy)
    requested_font = strategy.get("font_family")
    requested_font = requested_font if isinstance(requested_font, str) and requested_font else None
    labelled_before = set(labels)
    family, title, closing, labels = _fit_typography(
        font_covers,
        requested_font,
        title,
        closing,
        labels,
        extra_texts=[pin.text for pin in pins],
    )
    pins = _fit_pins(pins, family or requested_font or _DEFAULT_FONT, font_covers)
    for media_id in ordered_ids(ordered):
        if media_id in labelled_before - set(labels):
            dropped.append(media_id)
            dropped_reasons[media_id] = "no_fact"
    if not title:
        title_source = "none"

    if (
        approved_length_plan is None
        and song_duration_s is None
        and not visuals
        and not creator_duration_proven
        and view.target_duration_s is None
        and natural_length_owner(strategy) == "footage"
        and _closing_speech_span(ordered, sequence, strategy) is None
    ):
        # Reading-time holds remain protected while the source window is chosen
        # from the best analysed moment. Pins affect overlay placement, not the
        # footage duration, and are retained untouched in ``snapshot_kwargs``.
        sources, seat_minimums, seat_maximums = _spoken_sources(ordered, seats)
        minimums = {media_id: label.min_display_s for media_id, label in labels.items()}
        for media_id, floor in seat_minimums.items():
            minimums[media_id] = max(floor, minimums.get(media_id, 0.0))
        candidate = select_best_parts(
            sources,
            minimum_total_duration_s=MIN_TOTAL_S,
            minimum_duration_s=minimums,
            maximum_duration_s=seat_maximums,
        )
        candidate = _frame_bound_best_parts(candidate)
        if candidate.ok and candidate.duration_s >= MIN_TOTAL_S:
            best_parts = candidate
        else:
            best_parts_conflicts = [
                {
                    "code": conflict.code,
                    "requested_duration_s": conflict.requested_duration_s,
                    "available_duration_s": conflict.available_duration_s,
                    "message": conflict.message,
                }
                for conflict in candidate.conflicts
            ]
            if candidate.ok:
                best_parts_conflicts.append(
                    {
                        "code": "snapshot_min_duration",
                        "requested_duration_s": candidate.duration_s,
                        "available_duration_s": candidate.capacity_s,
                        "message": (
                            "The selected footage is shorter than the 3 second proposal minimum."
                        ),
                    }
                )
            raise ValueError(
                "The selected footage is too short or needs a constraint answer: "
                + str(best_parts_conflicts[0]["code"])
            )

    # ── cut lengths ──────────────────────────────────────────────────────────
    approved_windows = has_approved_windows
    # KRI-596: a long take may give several moments; each is its own cut, in plan order.
    windows_by_id: dict[str, list[BestPart]] = {}
    for part in best_parts.parts if best_parts else ():
        windows_by_id.setdefault(part.media_id, []).append(part)
    # KRI-796: "six moments, in this order: A, B, C ... the photo right after B". The
    # creator's moments are the take's approved windows, and a Visual they seated among
    # them plays there instead of after the whole take.
    moment_seating = (
        moment_seats(
            sequence,
            windows={
                clip.ref_id: len(windows_by_id[clip.media_id])
                for clip in ordered
                if clip.lane == "clip" and clip.media_id in windows_by_id
            },
            visuals={visual.ref_id for visual in visuals},
        )
        if sequence and approved_windows
        else None
    )
    wanted: list[int] = []
    capacity: list[int] = []
    short: list[str] = []
    for clip in ordered:
        label = labels.get(clip.media_id)
        approved_parts = windows_by_id.get(clip.media_id) if approved_windows else None
        if approved_parts:
            # The proposal already selected these exact source windows.  These
            # frame counts are only bookkeeping for the legacy code below;
            # cut construction continues to use the unrounded approved parts.
            want_s = sum(part.duration_s for part in approved_parts)
        elif clip.kind == "image" and mixed_media_timing is not None:
            want_s = mixed_media_hold_bounds("image", mixed_media_timing).preferred_s
        else:
            want_s = label.min_display_s if label is not None else DEFAULT_CUT_S
        want = int(math.ceil(want_s * FPS - 1e-9))
        if approved_parts:
            cap = want
        elif clip.kind == "image" and mixed_media_timing is not None:
            # The snapshot validator owns these photo bounds too.  Keep the
            # legacy minimum-duration grower from stretching a profiled still
            # past its approved maximum.
            image_bounds = mixed_media_hold_bounds("image", mixed_media_timing)
            cap = min(_capacity_frames(clip), int(math.floor(image_bounds.maximum_s * FPS)))
        else:
            cap = _capacity_frames(clip)
        if label is not None and cap < want:
            short.append(clip.media_id)
        wanted.append(min(want, cap))
        capacity.append(cap)
    labelled = [clip.media_id in labels for clip in ordered]
    floor_frames = int(math.ceil(MIN_TOTAL_S * FPS))
    # A strategy target is model/client data until the caller supplies
    # authoritative creator-duration proof. Brief timing is already trusted.
    target_s = view.target_duration_s or (
        _positive_number(strategy.get("target_duration_s")) if creator_duration_proven else None
    )
    target_frames = max(floor_frames, int(round(target_s * FPS))) if target_s else floor_frames
    # A creator-stated length is honoured even when it needs cuts longer than a
    # beat of attention (one long clip, a day vlog); a default fast montage is not.
    growth_ceiling = (
        list(capacity) if target_s else [min(cap, int(READING_MAX_S * FPS)) for cap in capacity]
    )
    # KRI-517: the creator asked to end on a clip that speaks, with its own audio. Its
    # cut holds the whole spoken line (never shrunk, never grown past it) and the other
    # cuts share the rest of the length. A line the creator's length cannot also fit
    # keeps the ordinary cut.
    speech_span = (
        _closing_speech_span(ordered, sequence, strategy) if song_duration_s is None else None
    )
    if speech_span is not None and not approved_windows:
        line = int(math.ceil((speech_span[1] - speech_span[0]) * FPS - 1e-9))
        held = max(wanted[-1], min(capacity[-1], line))
        if not target_s or sum(wanted) - wanted[-1] + held <= target_frames:
            wanted[-1] = held
            growth_ceiling[-1] = held
            labelled[-1] = True  # ``_shrink`` only takes frames from unlabelled cuts
        else:
            speech_span = None
    # KRI-585: without chosen windows, the welcome the spoken order moved forward is
    # heard whole, like the closing line above (a line the length cannot also fit keeps
    # the ordinary cut), and a creator-pinned closing clip whose words only introduce
    # someone stays on its quiet stretch, never longer than the stretch.
    opening_index: int | None = None
    quiet_index: int | None = None
    if not approved_windows and best_parts is None:
        refs_at = {clip.ref_id: index for index, clip in enumerate(ordered)}
        if seats.opening is not None and seats.opening[0] in refs_at:
            index = refs_at[seats.opening[0]]
            span = seats.opening[1]
            line = int(math.ceil((span[1] - span[0]) * FPS - 1e-9))
            held = max(wanted[index], min(capacity[index], line))
            if not target_s or sum(wanted) - wanted[index] + held <= target_frames:
                wanted[index] = held
                growth_ceiling[index] = held
                labelled[index] = True
                opening_index = index
        if (
            seats.quiet_close is not None
            and speech_span is None
            and ordered[-1].ref_id == seats.quiet_close[0]
        ):
            quiet_s = seats.quiet_close[1][1] - seats.quiet_close[1][0]
            quiet_frames = int(math.floor(quiet_s * FPS + 1e-6))
            if wanted[-1] <= quiet_frames:
                growth_ceiling[-1] = min(growth_ceiling[-1], quiet_frames)
                quiet_index = len(ordered) - 1
    total = sum(wanted) if approved_windows else _grow(wanted, growth_ceiling, target_frames)
    if not approved_windows and total < floor_frames:
        # Too short to be a video at all: use every frame the clips have.
        total = _grow(wanted, capacity, floor_frames)
        if total < floor_frames:
            raise ValueError("these clips are too short to make a montage")
    if not approved_windows and total > target_frames and target_s:
        # Longer than the creator asked for: only unlabelled cuts may shrink,
        # and never below the shortest cut a montage should have.
        total = _shrink(wanted, labelled, total, max(target_frames, floor_frames))
    song_dropped: list[str] = []
    if song_duration_s is not None and not approved_windows:
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
    approved_total_s = (
        sum(part.duration_s for part in best_parts.parts)
        if approved_windows and best_parts
        else total / FPS
    )
    if approved_total_s > MAX_PROPOSAL_DURATION_S:
        raise ValueError("these clips make a montage that is too long")

    # ── background song: window + beat-snapped cut lengths (ms) ──────────────
    cut_ms: list[int] | None = None
    song_plan: UserSongPlan | None = None
    song_receipt: dict[str, Any] = {}
    if song_duration_s is not None and not approved_windows:
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

    spans: list[tuple[str, float, float, bool]] = []  # media id, start, end, approved
    refs: list[MediaRef] = []
    slots: list[_Slot] = [
        (index, clip, frames, selected_window)
        for index, (clip, frames) in enumerate(zip(ordered, wanted, strict=True))
        for selected_window in windows_by_id.get(clip.media_id) or [None]
    ]
    if moment_seating is not None and moment_seating.after:
        slots = _seat_among_moments(slots, moment_seating)
    for index, clip, frames, selected_window in slots:
        duration = (
            selected_window.duration_s
            if selected_window is not None
            else cut_ms[index] / 1000
            if cut_ms is not None
            else frames / FPS
        )
        if clip.kind == "image":
            start, end = 0.0, round(duration, 3)
        elif selected_window is not None:
            start, end = selected_window.source_start_s, selected_window.source_end_s
        elif clip.duration_s < MIN_VIDEO_CUT_S:
            # Whole frames stop a fraction of a frame short of a 0.298s clip,
            # which the snapshot refuses: a clip this short is shown whole.
            start, end = 0.0, math.floor(clip.duration_s * 1000) / 1000
        else:
            closing_line = speech_span is not None and index == len(ordered) - 1
            if closing_line:
                start = speech_span[0]
            elif index == opening_index and seats.opening is not None:
                start = seats.opening[1][0]
            elif index == quiet_index and seats.quiet_close is not None:
                # Centred in the quiet stretch: as far from the words as it can be.
                quiet_start, quiet_end = seats.quiet_close[1]
                start = round(quiet_start + max(0.0, (quiet_end - quiet_start - duration) / 2), 3)
            else:
                start = _window_start_s(clip, duration)
            end = round(start + duration, 3)
            if end > clip.duration_s + 0.001:
                start = round(max(0.0, clip.duration_s - duration), 3)
                end = round(start + duration, 3)
        spans.append((clip.media_id, start, end, selected_window is not None))
    for clip in ordered:
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
    holds: dict[int, float] = {}
    freeze_record: dict[str, Any] = {}
    if freeze_frames and view.freeze is not None and song_duration_s is None:
        spans, holds, freeze_record = _ground_freeze(view.freeze, spans, ordered)
        if holds and target_s:
            # A creator-stated length already sized the cuts: the hold comes out of it.
            shrinkable = [
                not approved
                and not labelled[slots[k][0]]
                and slots[k][1].kind == "video"
                and k not in holds
                for k, (_media, _start, _end, approved) in enumerate(spans)
            ]
            spans, freeze_record = _reserve_freeze_hold(
                spans, holds, shrinkable, float(target_s), freeze_record
            )
    cuts = [
        FastMontageCut(
            cut_id=f"unified-cut-{index + 1}",
            media_id=media_id,
            source_start_s=start,
            source_end_s=end,
            output_duration_s=(
                ((end - start) if approved else round(end - start, 3)) + holds.get(index, 0.0)
            ),
            role="hook" if index == 0 else "payoff" if index == len(spans) - 1 else "build",
            transition="none",
            beat_align=False,
            **({"freeze_hold_s": holds[index]} if index in holds else {}),
        )
        for index, (media_id, start, end, approved) in enumerate(spans)
    ]
    if freeze_record.get("status") == "held":
        freeze_record["cut_id"] = cuts[freeze_record.pop("cut_index")].cut_id
    repeats = any(len(windows) > 1 for windows in windows_by_id.values())
    spoken_roles, opening_speech, closing_quiet = _spoken_records(ordered, cuts, seats)
    closing_speech, closing_quote = _closing_records(
        cuts,
        speech_span,
        approved_length_plan.get("closing_quote") if approved_length_plan is not None else None,
    )
    named_lines = _named_line_records(
        cuts, approved_length_plan.get("named_lines") if approved_length_plan is not None else None
    )
    # Keep the exact frame sum: rounding it to milliseconds can disagree with
    # the frame-aligned cuts by a third of a frame.
    total_s = sum(cut.output_duration_s for cut in cuts)
    ref_of = {clip.media_id: clip.ref_id for clip in ordered}
    pins = drawable_pins(pins, total_s, [cut.output_duration_s for cut in cuts])

    snapshot_kwargs: dict[str, Any] = {}
    if closing:
        snapshot_kwargs["closing_title"] = closing
    if pins:
        snapshot_kwargs["pinned_texts"] = pins
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
    if mixed_media_timing is not None:
        snapshot_kwargs["mixed_media_timing"] = mixed_media_timing
    # KRI-522: the brief is cumulative, so the creator's ask survives a later turn whose
    # strategy omits it; the strategy's own typed value is the fallback.
    title_animation = view.title_animation or normalize_title_animation(
        strategy.get("title_animation")
    )
    if title_animation is not None and title:
        snapshot_kwargs["title_animation"] = title_animation
    label_position = view.label_position or normalize_label_position(strategy.get("label_position"))
    if label_position is not None and labels:
        snapshot_kwargs["label_position"] = label_position
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
            goal=(editorial.get("viewer_goal", "") if editorial else ""),
            editorial_plan=editorial,
            pace="fast",
            duration_s=total_s,
            title=(title or SNAPSHOT_FALLBACK_TITLE)[:100],
            opening_title=title,
            media=refs,
            story_beats=_story_beats(cuts),
            fast_cuts=cuts,
            media_scope="all",
            selected_media_ids=[clip.media_id for clip in ordered],
            # Several moments of one take are distinct windows, never repeated footage.
            video_reuse_policy="distinct_windows" if repeats else "once",
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
        intent_outcomes=_intent_outcomes(
            strategy,
            clip_intents_enabled,
            ordered,
            labels,
            seats=moment_seating,
            cuts=[(ref_of[cut.media_id], cut.source_start_s) for cut in cuts],
        ),
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
        closing_speech=closing_speech,
        closing_quote=closing_quote,
        best_parts_receipt=best_parts.receipt() if best_parts is not None else [],
        clip_durations_s={
            clip.media_id: round(clip.duration_s, 3)
            for clip in ordered
            if clip.lane == "clip" and clip.duration_s > 0
        },
        best_parts_conflicts=best_parts_conflicts,
        spoken_roles=spoken_roles,
        opening_speech=opening_speech,
        closing_quiet=closing_quiet,
        named_lines=named_lines,
        freeze_frame=freeze_record,
    )


def _reserve_freeze_hold(
    spans: list[tuple[str, float, float, bool]],
    holds: Mapping[int, float],
    shrinkable: Sequence[bool],
    target_s: float,
    record: dict[str, Any],
) -> tuple[list[tuple[str, float, float, bool]], dict[str, Any]]:
    """KRI-828: make room for the hold inside the creator's stated length.

    Cuts were sized to ``target_s`` before the freeze was placed, so the hold would push
    the video past the length the creator asked for. The overshoot comes, whole frames at a
    time, first out of the ends of the other plain video cuts (never below the shortest a
    cut can be; never a photo, a chosen window or a held spoken line), then, only as far
    as the length check's tolerance requires, out of the motion before the frozen frame
    (the frame itself never moves). What still does not fit is recorded as
    ``length_over_s``.
    """
    total = sum(end - start for _media, start, end, _approved in spans) + sum(holds.values())
    excess = int(math.ceil((total - target_s) * FPS - 1e-6))
    if excess <= 0:
        return spans, record
    spans = [*spans]
    floor = int(math.ceil(MIN_VIDEO_CUT_S * FPS))
    room = {
        k: max(0, int(math.floor((end - start) * FPS + 1e-6)) - floor)
        for k, (_media, start, end, _approved) in enumerate(spans)
        if shrinkable[k]
    }
    taken = dict.fromkeys(room, 0)
    while excess and any(room[k] > taken[k] for k in room):
        # One frame at a time from the cut with the most room left: an even trim.
        k = max(room, key=lambda key: (room[key] - taken[key], -key))
        taken[k] += 1
        excess -= 1
    for k, frames in taken.items():
        if frames:
            media, start, end, approved = spans[k]
            spans[k] = (media, start, round(end - frames / FPS, 3), approved)
    # The chosen moment's own lead-in is the creator's too: it only gives what is still
    # needed for the length to read as asked (within the receipt's tolerance, a frame
    # inside it), never down to the exact target.
    excess = max(0, excess - int(math.floor(target_s * _LENGTH_TOLERANCE * FPS)) + 1)
    for k in holds:
        if not excess:
            break
        media, start, end, approved = spans[k]
        motion_room = max(0, int(math.floor((end - start - _FREEZE_MIN_MOTION_S) * FPS + 1e-6)))
        frames = min(excess, motion_room)
        if frames:
            spans[k] = (media, round(start + frames / FPS, 3), end, approved)
            excess -= frames
    if excess:
        record = {**record, "length_over_s": round(excess / FPS, 3)}
    return spans, record


# KRI-828: how far a creator's stated length may stretch and still read as asked; the
# same share the brief's length check allows (``brief_checks.DURATION_TOLERANCE``).
_LENGTH_TOLERANCE = 0.10
# KRI-828: a freeze's grounding. Words this short or this generic never pick a moment.
_FREEZE_MATCH_MIN_LEN = 3
_FREEZE_PREFIX_LEN = 4
_FREEZE_MIN_MOTION_S = 0.3


def _freeze_words(text: str) -> set[str]:
    folded = loose_text(str(text))
    return {
        word
        for word in "".join(ch if ch.isalnum() else " " for ch in folded).split()
        if len(word) >= _FREEZE_MATCH_MIN_LEN
    }


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _FREEZE_PREFIX_LEN and longer.startswith(shorter)


def _ground_freeze(
    spec: FreezeSpec,
    spans: list[tuple[str, float, float, bool]],
    clips: Sequence[UnifiedClip],
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]]:
    """Where the creator's freeze lands: one cut whose window ends on the frozen frame.

    The creator's words for the moment ("Atakan's bite") are matched against the analysed
    moments of the clips in the edit (``analysis.best_moments[].description``, e.g. "man
    takes first bite of dessert"). Those descriptions only choose a TIME; they never become
    text. Exactly one best-matching moment grounds the freeze at its midpoint; the cut of
    that clip nearest to it is re-anchored to end there (same length of motion, never
    overlapping another cut of the same take), and holds for ``spec.hold_s``.

    No match, or a tie between different moments, is ``not_grounded``: no frame is
    guessed, and the receipt says so.
    """
    record: dict[str, Any] = {
        "requirement_id": spec.requirement_id,
        "hold_s": spec.hold_s,
        "hold_stated": spec.hold_stated,
        **(
            {"hold_requested_s": spec.hold_requested_s} if spec.hold_requested_s is not None else {}
        ),
    }
    target = {word for word in spec.target_words if len(word) >= _FREEZE_MATCH_MIN_LEN}
    by_id = {clip.media_id: clip for clip in clips}
    candidates: list[tuple[int, str, float, float]] = []  # score, media id, start, end
    for media_id in dict.fromkeys(media for media, *_rest in spans):
        clip = by_id.get(media_id)
        if clip is None or clip.kind != "video" or not isinstance(clip.analysis, Mapping):
            continue
        for row in clip.analysis.get("best_moments") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                start, end = float(row["start_s"]), float(row["end_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(start) and math.isfinite(end) and end > start >= 0):
                continue
            words = _freeze_words(str(row.get("description") or ""))
            score = sum(1 for word in target if any(_same_word(word, other) for other in words))
            if score:
                candidates.append((score, media_id, start, end))
    best = max((row[0] for row in candidates), default=0)
    top = {(media, start, end) for score, media, start, end in candidates if score == best}
    if best == 0 or len(top) != 1:
        return spans, {}, {**record, "status": "not_grounded"}
    media_id, moment_start, moment_end = next(iter(top))
    clip = by_id[media_id]
    frame = round((moment_start + moment_end) / 2, 3)
    frame = min(frame, math.floor((clip.duration_s - 0.05) * 1000) / 1000)
    owned = [
        (index, start, end)
        for index, (media, start, end, _approved) in enumerate(spans)
        if media == media_id
    ]
    if not owned or frame <= 0:
        return spans, {}, {**record, "status": "not_grounded"}
    index, start, end = min(owned, key=lambda row: abs((row[1] + row[2]) / 2 - frame))
    earlier = [e for i, s, e in owned if i != index and s < frame]
    if any(s < frame < e for i, s, e in owned if i != index):
        # Another cut of the take already plays that frame: holding it here would repeat it.
        return spans, {}, {**record, "status": "not_grounded"}
    new_start = round(max(0.0, frame - (end - start), *earlier), 3)
    if frame - new_start < _FREEZE_MIN_MOTION_S:
        return spans, {}, {**record, "status": "not_grounded"}
    approved = spans[index][3]
    spans = [*spans]
    spans[index] = (media_id, new_start, frame, approved)
    return (
        spans,
        {index: spec.hold_s},
        {
            **record,
            "status": "held",
            "media_id": media_id,
            "cut_index": index,
            "frame_source_s": frame,
            "grounding": "analysed_moment",
            "moment_source_s": [round(moment_start, 3), round(moment_end, 3)],
        },
    )


def _spoken_sources(
    ordered: Sequence[UnifiedClip], seats: SpokenSeats
) -> tuple[list[UnifiedClip], dict[str, float], dict[str, float]]:
    """KRI-585 for the footage selector: the seated windows lead each clip's moments.

    The welcome's line is its clip's strongest moment and its floor (heard whole); the
    quiet closing stretch is its clip's strongest moment and its ceiling. Copies only:
    the clips the plan records keep their own analysis.
    """
    seated: dict[str, tuple[tuple[float, float], bool]] = {}
    if seats.opening is not None:
        seated[seats.opening[0]] = (seats.opening[1], False)
    if seats.quiet_close is not None:
        seated[seats.quiet_close[0]] = (seats.quiet_close[1], True)
    if not seated:
        return list(ordered), {}, {}
    sources: list[UnifiedClip] = []
    minimums: dict[str, float] = {}
    maximums: dict[str, float] = {}
    for clip in ordered:
        seat = seated.get(clip.ref_id)
        if seat is None:
            sources.append(clip)
            continue
        (start, end), quiet = seat
        analysis = dict(clip.analysis) if isinstance(clip.analysis, Mapping) else {}
        moments = analysis.get("best_moments")
        analysis["best_moments"] = [
            {"start_s": start, "end_s": end, "energy": SPOKEN_SEAT_ENERGY},
            *(moments if isinstance(moments, list) else []),
        ]
        sources.append(replace(clip, analysis=analysis))
        length = round(end - start, 3)
        if quiet:
            minimums[clip.media_id] = min(length, DEFAULT_CUT_S)
            maximums[clip.media_id] = length
        else:
            minimums[clip.media_id] = length
    return sources, minimums, maximums


def _spoken_records(
    ordered: Sequence[UnifiedClip], cuts: Sequence[FastMontageCut], seats: SpokenSeats
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """(spoken_roles, opening_speech, closing_quiet), each claimed only when the cuts do it."""
    if not seats.lines or not cuts:
        return {}, {}, {}
    media_of = {clip.ref_id: clip.media_id for clip in ordered}
    roles = {media_of[ref]: line.role for ref, line in seats.lines.items() if ref in media_of}
    opening: dict[str, Any] = {}
    if seats.opening is not None:
        media_id = media_of.get(seats.opening[0])
        cut = next((c for c in cuts[:-1] if c.media_id == media_id), None)
        if cut is not None and window_holds(
            (cut.source_start_s, cut.source_end_s), seats.opening[1]
        ):
            opening = _cut_record(cut)
    quiet: dict[str, Any] = {}
    last = cuts[-1]
    if (
        seats.quiet_close is not None
        and last.media_id == media_of.get(seats.quiet_close[0])
        and window_inside((last.source_start_s, last.source_end_s), seats.quiet_close[1])
    ):
        quiet = {**_cut_record(last), "reason": "introduction"}
    return roles, opening, quiet


def _cut_record(cut: FastMontageCut) -> dict[str, Any]:
    return {
        "media_id": cut.media_id,
        "source_start_s": cut.source_start_s,
        "source_end_s": cut.source_end_s,
    }


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
    *,
    extra_texts: Sequence[str] = (),
) -> tuple[str | None, str | None, str | None, dict[str, ClipLabel]]:
    """Pick the first bundled font that can draw every string, keeping Unicode.

    Missing legacy glyphs never change confirmed wording: the phone compiler
    uses shaped text, including the system's color emoji face.
    """
    if covers is None:
        return requested, title, closing, labels
    import regex  # noqa: PLC0415 - Unicode grapheme/emoji properties

    texts = [
        text
        for text in (title, closing, *(label.text for label in labels.values()), *extra_texts)
        if text
    ]
    from app.schemas.phone_text_style import CURATED_PHONE_FONTS  # noqa: PLC0415
    from app.services.phone_text_styling import current_phone_text_policy  # noqa: PLC0415

    policy = current_phone_text_policy()
    if policy:
        # Coverage is role-scoped in the shared pass. This early planner adapter
        # must not strip a label because a separately requested title font lacks
        # its glyphs. Use a drawable provisional face for the compiler dry run.
        chosen = next(
            (font for font in CURATED_PHONE_FONTS if all(covers(font, text) for text in texts)),
            None,
        )
        if chosen is None:
            from app.pipeline.phone_text_style import _style_decline

            raise _style_decline("The eligible fonts cannot draw all text. Choose another font.")
        return chosen, title, closing, labels
    # Color emoji are supplied by iOS, not the bundled face. Let the face
    # selection still choose DM Sans for an ordinary arrow beside an emoji.
    font_texts = [
        "".join(
            grapheme
            for grapheme in regex.findall(r"\X", text)
            if not regex.search(r"\p{Emoji_Presentation}|\p{Emoji}\uFE0F|\u20E3", grapheme)
        )
        for text in texts
    ]
    # KRI-785: a requested face missing LETTERS of the text (Turkish ğ ş İ ı in
    # Satisfy) tries its glyph-safe partner next, the face the cloud renderer
    # and the iOS compiler swap to, before the unrelated default.
    partner = None
    if requested:
        from app.pipeline.font_coverage import glyph_safe_family, missing_glyphs  # noqa: PLC0415

        joined = " ".join(font_texts)
        if any(ch.isalpha() for ch in missing_glyphs(requested, joined)):
            partner = glyph_safe_family(requested, joined)
    candidates = list(
        dict.fromkeys(f for f in (requested, partner, _DEFAULT_FONT, *_FALLBACK_FONTS) if f)
    )
    chosen = next(
        (family for family in candidates if all(covers(family, text) for text in font_texts)), None
    )
    if chosen is None:
        chosen = candidates[0]
        fixed: dict[str, ClipLabel] = {}
        for media_id, label in labels.items():
            text = (
                label.text
                if label.provenance == "creator"
                else _strip_uncovered(label.text, chosen, covers)
            )
            if text:
                fixed[media_id] = label.model_copy(
                    update={"text": text, "min_display_s": min_display_s(len(text))}
                )
        labels = fixed
    keep = requested is not None or chosen != _DEFAULT_FONT
    return (chosen if keep else None), title, closing, labels


def _pinned_texts(strategy: Mapping[str, Any]) -> list[PinnedText]:
    """KRI-523: the creator's corner text, as confirmed on the strategy."""
    return pins_from_strategy(strategy)


def _fit_pins(
    pins: list[PinnedText], family: str, covers: Callable[[str, str], bool] | None
) -> list[PinnedText]:
    """Pins are confirmed copy; the phone's shaped renderer resolves their glyphs."""
    return pins


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


def _chapter_captions(strategy: Mapping[str, Any]) -> list[tuple[str, str, list[str]]]:
    """(group id, the creator's chapter title, member media ids) per caption intent.

    The group id is the intent's own id: clips of one chapter that sit next to each
    other in the final order share ONE continuous title instead of a copy per clip.
    """
    out: list[tuple[str, str, list[str]]] = []
    for index, intent in enumerate(strategy.get("resolved_clip_intents") or []):
        if not _is_resolved(intent, "caption") or intent.get("creator_text"):
            continue
        text = _nfc(intent.get("caption_text"))
        if text and intent.get("caption_grounding") == "creator_text":
            group_id = _nfc(intent.get("intent_id")) or f"caption-{index + 1}"
            out.append((group_id, text, _members(intent)))
    return out


def _chapter_group_ids(
    strategy: Mapping[str, Any], enabled: bool
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """(chapter titles, described titles): media_id -> (group id, text) of a shared title.

    The first maps the chapter-name captions ``_intent_labels`` prints; the second maps
    the creator's own words matched to SEVERAL clips (``_described_shot_labels``), which
    are the same words on every member and so also read as one title across neighbours.
    """
    if not enabled:
        return {}, {}
    chapters: dict[str, tuple[str, str]] = {}
    for group_id, text, members in _chapter_captions(strategy):
        for media_id in members:
            chapters[media_id] = (group_id, text)
    described: dict[str, tuple[str, str]] = {}
    for index, intent in enumerate(strategy.get("resolved_clip_intents") or []):
        if not _is_resolved(intent, "caption") or not intent.get("creator_text"):
            continue
        if intent.get("caption_grounding") != "creator_text":
            continue
        text = _nfc(intent.get("caption_text"))
        members = _members(intent)
        if not text or len(members) < 2:
            continue
        group_id = _nfc(intent.get("intent_id")) or f"caption-{index + 1}"
        for media_id in members:
            described.setdefault(media_id, (group_id, text))
    return chapters, described


def _intent_labels(strategy: Mapping[str, Any], enabled: bool) -> dict[str, tuple[str, bool]]:
    """media_id -> (text, creator_wrote_it) from server-verified label intents."""
    if not enabled:
        return {}
    rows, placeholders = _label_rows(strategy)
    # KRI-282: "a text for the pub" prints the chapter's own name, in the creator's
    # words, on that chapter's clips, over a broader sport tag.
    for _group_id, text, members in _chapter_captions(strategy):
        for media_id in members:
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
    *,
    seats: MomentSeats | None = None,
    cuts: Sequence[tuple[str, float]] = (),
) -> list[dict[str, Any]]:
    """What each requested group / sport label / chapter text did in this plan (KRI-282).

    Computed from the finished plan, so a receipt can only report what rendered:
    ``status`` is ``met`` / ``partial`` / ``not_possible`` like a requirement receipt.
    The person placeholder is left out (its requirement receipt covers it).
    """
    if not enabled:
        return []
    sequence = _sequence_outcomes(
        _sequence_intents(strategy, enabled), ordered, seats=seats, cuts=cuts
    )
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


def _title(
    strategy: Mapping[str, Any], view: BriefView, label_texts: Sequence[str] = ()
) -> tuple[str | None, str]:
    """The opening title and where it came from.

    ``label_texts`` are the per-clip labels the montage prints. A brief literal that only
    lists them ("Bölüm başlıkları: Sabah, Üniversite, Öğle arası, Spor, Akşam") asked for
    chapter titles, which the labels already are: it is never also an opening title (KRI-545).
    """
    if "opening_title" in (strategy.get("omitted_copy_targets") or []):
        return None, "none"
    confirmed = _nfc(strategy.get("opening_title"))
    if confirmed:
        return confirmed, "creator"

    def titles(literal: str | None) -> bool:
        return bool(literal) and chapter_list(literal, label_texts) is None

    pins = _pinned_texts(strategy)
    if pins:
        # KRI-523: the creator's "title" words were pinned to a corner for the whole video
        # (the brief records them as a title/global literal too). Burning them again as a
        # centred opening title would show every pinned line twice, and a labelled edit shows
        # only confirmed copy. A genuinely different title literal still stands.
        pinned = {fold_text(pin.text) for pin in pins}
        literal = _nfc(view.title_literal)
        if titles(literal) and fold_text(literal) not in pinned:
            return literal, "creator"
        return None, "none"
    if titles(view.title_literal):
        return _nfc(view.title_literal), "creator"
    start = _first(view.facts, _START_KEYS)
    end = _first(view.facts, _END_KEYS)
    if titles(view.global_literal):
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


def title_source_exists(strategy: Mapping[str, Any], brief: Any) -> bool:
    """Would ``plan_unified_montage`` burn an opening title for this strategy + brief?

    The clarification gate asks "what are the title's words?" only when this is False, so
    the question and the render read the same ``_title`` and cannot disagree. Before the
    plan exists, the labels it will print are the creator's own per-clip words: the brief's
    clip literals, confirmed shot labels and the resolved clip intents (read as enabled, the
    phone path's production setting).
    """
    view = brief_view(brief)
    labels = [
        *view.clip_literals.values(),
        *(_nfc(text) for text in strategy.get("shot_labels") or []),
        *(text for text, _creator in _intent_labels(strategy, True).values()),
        *_described_shot_labels(strategy, True).values(),
    ]
    return _title(strategy, view, labels)[0] is not None


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
    "title_source_exists",
]
