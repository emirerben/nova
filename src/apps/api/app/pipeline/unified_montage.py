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
  every phone montage with a photo was refused at dispatch. Under a filming-order
  ask a dated Visual joins the chronology by its own capture time instead
  (KRI-1069), the order the render contract pins.

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

from app.config import settings
from app.kria.asset_holds import (
    AssetHold,
    hold_outcomes,
    hold_requirements,
    holds_from_requirements,
)
from app.kria.brief_route import (
    END_KEYS,
    START_KEYS,
    chapter_list,
    first_text,
    fold_text,
    wants_filming_time_text,
    wants_hour_only_text,
)
from app.kria.freeze_ask import (
    CONFLICT_FREEZE_FRAME,
    POSITION_LAST,
    FreezeSpec,
    live_freezes,
    match_moments,
    media_key,
    parse_freeze_option,
    times_one_moment,
)
from app.kria.speed_ask import SpeedSpec, live_speeds
from app.kria.text_sequence import is_text_sequence
from app.kria.text_shape_ask import CURVED, CURVED_TEXT_BEND, text_shape
from app.pipeline.best_parts import (
    MAX_BEST_PARTS_DURATION_S,
    BestPart,
    BestPartsResult,
    select_best_parts,
)
from app.pipeline.font_coverage import drop_color_emoji
from app.pipeline.hidden_parts import (
    HiddenPart,
    HiddenPartShown,
    check_windows,
    hidden_parts_from_strategy,
    keep_out,
    parts_by_media,
    shows_hidden,
    unsettled_hidden_parts,
    usable_s,
)
from app.pipeline.pinned_text import drawable_pins, pins_from_strategy
from app.pipeline.story_direction import story_floor_for
from app.schemas.clip_intents import PLACEHOLDER_LABEL_TEXT
from app.schemas.edit_proposal import (
    CREATOR_SELECTED_ORIENTATION_REASON,
    MAX_PROPOSAL_DURATION_S,
    MIN_PROPOSAL_DURATION_S,
    SNAPSHOT_FALLBACK_TITLE,
    ClipLabel,
    CreatorStillHold,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    PinnedText,
    StoryBeat,
    canonical_media_digest,
    mixed_media_hold_bounds,
    pace_hold_bounds,
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
    UNCONFIRMED_MEMBER_ORDER,
    MomentSeats,
    SequenceGroupKey,
    apply_sequence,
    moment_seats,
    row_relations,
    seat_owners,
    sequence_rows,
    unconfirmed_order_groups,
)
from app.services.creator_omissions import creator_omission_outcomes
from app.services.length_policy import (
    direction_not_built,
    effective_pace,
    natural_length_owner,
    route_direction,
    stated_pace,
    story_direction,
)
from app.services.order_mode import order_mode, seeded_permutation
from app.services.order_mode import random_order_seed as order_seed_of
from app.services.spoken_quotes import is_outro_hallucination
from app.services.spoken_roles import (
    SpokenClip,
    SpokenSeats,
    speech_ordered,
    spoken_seats,
    window_holds,
    window_inside,
)
from app.text_fold import loose_words

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


def _text_key(value: object) -> str:
    """A creator's words without case, accents or punctuation ("Full-time!" == "Full time")."""
    return " ".join(loose_words(_nfc(value)))


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
    # Leverkusen M1 run 2 #3: no order ask is live, but the last basis the creator let go
    # of (the simpler version, "Continue without a fixed order") was filming time. Dropping
    # it waived the check, not the wish: the montage still plays by filming time where the
    # footage is dated, and nothing claims or verifies it (see ``plan_unified_montage``).
    kept_filming_order: bool = False
    # KRI-585: an order requirement says "the order I added them", so what the clips
    # say never moves one (filming-time keys set ``order_by_capture`` instead).
    order_basis_stated: bool = False
    # 29 Ekim: "in random order" carries the seed stored on the requirement, so every
    # re-plan and re-render draws the same permutation; "any order" needs no field.
    random_order_seed: int | None = None
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
    # Prompt fidelity (animation): the animation a live text ask names at ANY scope, and the
    # corner a global text ask names. ``text_animation`` is the creator's NAMED edge for the
    # whole text (an unnamed "with animations" is `animation_requested` alone: the creation text
    # pass gives it the default entrance). Both stay None with `text_animation_ask_enabled` off.
    text_animation: str | None = None
    animation_requested: bool = False
    text_position: str | None = None
    # KRI-828 / KRI-899: every live freeze the creator asked for (corroborated by their
    # words), a later correction already folded into the freeze it changes
    # (`freeze_ask.live_freezes`).
    freezes: tuple[FreezeSpec, ...] = ()
    # KRI-689: every live clip-speed ask (`speed_ask.live_speeds`): the rate and the creator's
    # words for the footage it changes.
    speeds: tuple[SpeedSpec, ...] = ()
    # KRI-824: live ``timing`` / ``clip:<id>`` requirements, resolved against the plan's
    # own photos by ``holds_from_requirements``; only server-stamped ones apply.
    hold_requirements: tuple[Any, ...] = ()
    # KRI-1029: the texts the creator asked curved (`text_shape_ask`): a title-scope ask,
    # a per-clip rule, and the folded words of every curved literal (whichever text draws
    # them: "curved title on the palms" is the palms clip's label).
    curved_title: bool = False
    curved_labels: bool = False
    curved_texts: frozenset[str] = frozenset()
    # KRI-1124: the words (`_text_key`) of text requirements the creator took back
    # (superseded), minus any a live text requirement asks for again. The strategy's clip
    # intents are re-derived from the whole thread every turn and never learn of the
    # withdrawal, so `_without_withdrawn_texts` leaves those words out of the plan.
    withdrawn_texts: frozenset[str] = frozenset()


def brief_view(brief: Any) -> BriefView:
    """Reduce a ``CreativeBrief`` (duck-typed, see ``app.kria.brief``) to a view."""
    if brief is None:
        return BriefView()
    wants_text = False
    wants_order = False
    order_capture = False
    basis_stated = False
    random_seed: int | None = None
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
    curved_title = curved_labels = False
    curved_texts: set[str] = set()
    live = brief.live()
    animation_requested = False
    text_animation: str | None = None
    text_position: str | None = None
    if settings.text_animation_ask_enabled:
        from app.kria.animation_ask import (  # noqa: PLC0415
            animation_targets,
            requested_text_position,
        )

        asks = animation_targets(live)
        animation_requested = bool(asks)
        text_animation = next((a.edge for t, a in asks if t == "all" and a.named), None)
        text_position = requested_text_position(live)
    freezes = live_freezes(live)
    speeds = live_speeds(live)
    withdrawn_texts = {
        _text_key(req.literal)
        for req in brief.requirements
        if not req.live and req.kind == "text" and req.literal
    } - {_text_key(req.literal) for req in live if req.kind == "text" and req.literal}
    withdrawn_texts.discard("")
    for req in live:
        for key, value in (req.facts or {}).items():
            if value not in (None, "") and key not in facts:
                facts[str(key)] = value
        if req.kind == "text":
            if text_shape(req) == CURVED:
                curved_title = curved_title or req.scope == "title"
                curved_labels = curved_labels or req.scope == "per_clip"
                if req.literal:
                    curved_texts.add(fold_text(_nfc(req.literal)))
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
        elif is_text_sequence(req) and req.literal and req.scope in ("title", "global"):
            # 29 Ekim: words shown a piece at a time are the opening title, split by the
            # creation text pass (`compose_creation_text`). Without this bar the pass had
            # nothing to split, so every such render failed as `creation_text_failed`. The
            # literal is the exact wording: never decorated with a route like a `global`
            # text, and a `title` text literal keeps the bar.
            title_literal = title_literal or _nfc(req.literal)
        elif req.kind == "order":
            wants_order = True
            key = str((req.facts or {}).get("key") or (req.facts or {}).get("by") or "")
            if key.casefold() in _CAPTURE_ORDER_KEYS:
                order_capture = True
            from app.services.choice_questions import ATTACHMENT_ORDER_KEY  # noqa: PLC0415

            # A "narrative" or a first/last-clip key is not a basis: it is what the
            # creator's own sequence rows (and the clips' words, KRI-585) decide.
            basis_stated = basis_stated or key.casefold() == ATTACHMENT_ORDER_KEY
            if order_mode(req) == "random":
                random_seed = order_seed_of(req)
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
        kept_filming_order=not wants_order and _dropped_filming_order(brief),
        order_basis_stated=basis_stated,
        random_order_seed=None if order_capture else random_seed,
        per_clip_text_is_time=wants_time,
        time_format="hour" if hour_only else "hh_mm",
        title_literal=title_literal,
        global_literal=global_literal,
        facts=facts,
        target_duration_s=target,
        title_animation=title_animation,
        label_position=label_position,
        placeholder_labels=placeholder_labels,
        text_animation=text_animation,
        animation_requested=animation_requested,
        text_position=text_position,
        freezes=freezes,
        speeds=speeds,
        hold_requirements=hold_requirements(brief),
        curved_title=curved_title,
        curved_labels=curved_labels,
        curved_texts=frozenset(curved_texts),
        withdrawn_texts=frozenset(withdrawn_texts),
    )


def _dropped_filming_order(brief: Any) -> bool:
    """Was the last order basis the brief superseded filming time (``kept_filming_order``)?

    A dropped upload-order or random-order ask written after it wins, so a creator who
    moved away from filming order before letting go of order altogether keeps that. So does
    a filming-order ask the CREATOR removed (``SUPERSEDED_BY_FACT``): a server waiver
    waives the check, not the wish, but the creator's own withdrawal withdraws the wish.
    A removal stored with no mark (before it existed) reads as a waiver.
    """
    from app.kria.brief import SUPERSEDED_BY_FACT  # noqa: PLC0415 - pipeline imports kria
    from app.services.choice_questions import ATTACHMENT_ORDER_KEY  # noqa: PLC0415

    filming = False
    for req in brief.requirements:
        if req.live or req.kind != "order":
            continue
        key = str((req.facts or {}).get("key") or (req.facts or {}).get("by") or "").casefold()
        if key in _CAPTURE_ORDER_KEYS:
            filming = (req.facts or {}).get(SUPERSEDED_BY_FACT) != "creator"
        elif key == ATTACHMENT_ORDER_KEY or order_mode(req) == "random":
            filming = False
    return filming


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
    # 29 Ekim: the seed a "random order" plan used and the clips it shuffled, so the receipt
    # can prove the result is a permutation of exactly the selected clips.
    random_order_seed: int | None = None
    random_order_source_ids: list[str] = field(default_factory=list)
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
    # KRI-828 / KRI-899: the creator's freezes as planned, one row per frozen moment:
    # {requirement_id, requirement_ids (every live ask it answers, corrections included),
    # status ("held" / "not_grounded"), media_id, cut_id, frame_source_s, hold_s, hold_basis,
    # grounding}. Empty without a freeze ask (or when the route cannot draw one), so every
    # earlier record is unchanged.
    freeze_frames: list[dict[str, Any]] = field(default_factory=list)
    # KRI-824: each photo the creator gave its own seconds: {media_id, hold_s,
    # requirement_id, authority, rendered_s}. Empty without such an ask, so every earlier
    # record is unchanged.
    asset_holds: list[dict[str, Any]] = field(default_factory=list)
    # KRI-689: the creator's clip-speed asks as planned, one row per ask: {requirement_id,
    # status ("applied" / "locked" / "not_grounded" / "photo" / "ramp" / "replaced"), rate,
    # rate_requested, rate_stated, cut_ids, cut_positions (1-based, screen order), media_ids,
    # grounding, reason, shortened, replaced_by}. Empty without a speed ask, so every earlier
    # record is unchanged; `brief_checks` reads a record WITHOUT the key as a plan that could
    # not retime (KRI-689's original "Couldn't").
    clip_speeds: list[dict[str, Any]] = field(default_factory=list)
    # KRI-1030: the first clip whose own label carries the opening title's words, so the
    # title is not drawn twice. None (every plan without the duplicate) is not recorded.
    title_shown_as_label: str | None = None
    # KRI-1029: one row per drawn text the creator asked curved: {text, role ("title" /
    # "label"), media_id, bend (None = straight), reason (None / "phone" / "animation")}.
    # Empty without a curve ask, so every earlier record is unchanged.
    text_shapes: list[dict[str, Any]] = field(default_factory=list)
    # Leverkusen M2: each part of a clip the creator asked never to show, as planned:
    # {media_id, edge, seconds, shown_from_s, shown_to_s} (the earliest and latest source
    # second any cut of that clip plays). Empty without such an ask, so every earlier record
    # is unchanged.
    hidden_parts: list[dict[str, Any]] = field(default_factory=list)
    # The direction the strategy asked for when this route built a different one (it builds
    # hard-cut fast montages only): the plan was built as ``snapshot.direction`` and a receipt
    # may say so. None (every plan whose strategy direction was built) is not recorded.
    requested_direction: str | None = None
    # Text-explainer unit: a story direction (text_explainer / guided_story) built as story
    # beats lists how many; ``story_fallback`` is the fast plan it was built from (the worker
    # falls back to it if the text step cannot place the story's text), and
    # ``direction_fallback`` is the reason a story direction was cut as a fast montage
    # (`story_direction.fallback_note`). All empty for every other plan, so records stay as is.
    story_beats: int | None = None
    story_fallback: UnifiedMontagePlan | None = None
    direction_fallback: str | None = None

    @property
    def cuts(self) -> list[FastMontageCut]:
        """The planned cuts (clip, source window, length) in play order, whatever layout the
        snapshot has: a story plan keeps the fast plan's cuts it was built from."""
        return list((self.story_fallback or self).snapshot.fast_cuts or [])

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
        shuffled = (
            {
                "random_order": {
                    "seed": self.random_order_seed,
                    "source_ids": list(self.random_order_source_ids),
                }
            }
            if self.random_order_seed is not None
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
        song = {"user_song": dict(self.song_receipt)} if self.song_receipt else {}
        speech = {"closing_speech": dict(self.closing_speech)} if self.closing_speech else {}
        quote = {"closing_quote": dict(self.closing_quote)} if self.closing_quote else {}
        named = {"named_lines": [dict(row) for row in self.named_lines]} if self.named_lines else {}
        freeze = (
            {"freeze_frames": [dict(row) for row in self.freeze_frames]}
            if self.freeze_frames
            else {}
        )
        held = {"asset_holds": [dict(row) for row in self.asset_holds]} if self.asset_holds else {}
        speeds = (
            {"clip_speeds": [dict(row) for row in self.clip_speeds]} if self.clip_speeds else {}
        )
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
        shown = (
            {"title_shown_as_label": self.title_shown_as_label} if self.title_shown_as_label else {}
        )
        shapes = (
            {"text_shapes": [dict(row) for row in self.text_shapes]} if self.text_shapes else {}
        )
        hidden = (
            {"hidden_parts": [dict(row) for row in self.hidden_parts]} if self.hidden_parts else {}
        )
        direction_asked = (
            {"requested_direction": self.requested_direction} if self.requested_direction else {}
        )
        if self.story_beats is not None:
            direction_asked["story"] = {
                "direction": self.snapshot.direction,
                "beats": self.story_beats,
            }
        if self.direction_fallback:
            direction_asked["direction_fallback"] = self.direction_fallback
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
            **shuffled,
            **zone,
            **named,
            **freeze,
            **held,
            **speeds,
            **song,
            **speech,
            **quote,
            **best_parts,
            **durations,
            **best_parts_conflicts,
            **captions,
            **spoken,
            **shown,
            **shapes,
            **hidden,
            **direction_asked,
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


# ``CreatorStillHold.requirement_id`` of a photo held by the photos' group length.
GROUP_STILL_HOLD_ID = "photos"


def group_still_hold(
    media_id: str, mixed_media_timing: MixedMediaTimingProfile | None
) -> AssetHold | None:
    """KRI-825: the photos' group length as one photo's creator-owned hold, else None."""
    seconds = mixed_media_timing.image_hold_s if mixed_media_timing is not None else None
    if seconds is None:
        return None
    return AssetHold(media_id, round(float(seconds), 3), GROUP_STILL_HOLD_ID, "explicit")


def unheld_still_frames(mixed_media_timing: MixedMediaTimingProfile | None) -> int:
    """Frames a photo nobody gave a length plays: the AI's cadence, else a default cut.

    The one rule both this planner and the approved length budget
    (``kria.planner._enrich_best_parts_snapshot``) read, so they cannot drift (KRI-825).
    """
    want_s = (
        mixed_media_hold_bounds("image", mixed_media_timing).preferred_s
        if mixed_media_timing is not None
        else DEFAULT_CUT_S
    )
    return int(math.ceil(want_s * FPS - 1e-9))


def _capacity_frames(clip: UnifiedClip, hidden: Sequence[HiddenPart] = ()) -> int:
    if clip.kind == "image":
        return int(round(STILL_MAX_S * FPS))
    if hidden:
        # Leverkusen M2: a clip with a part the creator hid can only grow into what it may
        # still show, or the keep-out step would find no room for the cut.
        return max(1, int(math.floor(usable_s(float(clip.duration_s), hidden) * FPS + 1e-6)))
    return max(1, int(math.floor((float(clip.duration_s) + 0.001) * FPS + 1e-6)))


def _hidden_by_media(
    ordered: Sequence[UnifiedClip], parts: Sequence[HiddenPart]
) -> dict[str, tuple[HiddenPart, ...]]:
    """Each clip's hidden parts, keyed by the id its cuts carry (``media_id``).

    The resolved intents name clips by manifest id (``ref_id``); a part names a video, so a
    photo never gets one."""
    by_ref = parts_by_media(parts)
    return {
        clip.media_id: tuple(replace(part, media_id=clip.media_id) for part in by_ref[clip.ref_id])
        for clip in ordered
        if clip.kind == "video" and clip.ref_id in by_ref
    }


def _kept_out(
    spans: list[tuple[str, float, float, bool]],
    hidden: Mapping[str, Sequence[HiddenPart]],
    durations: Mapping[str, float],
    *,
    movable: Callable[[int], bool],
) -> list[tuple[str, float, float, bool]]:
    """``spans`` with every movable window slid out of its clip's hidden parts (same length).

    A window that may not move (an approved or a frozen one) or has no room left raises
    ``HiddenPartShown``: the plan is refused, never rendered with the part."""
    out = list(spans)
    for index, (media_id, start, end, approved) in enumerate(spans):
        parts = hidden.get(media_id)
        if not parts or not shows_hidden(start, end, durations[media_id], parts):
            continue
        moved = keep_out(start, end, durations[media_id], parts) if movable(index) else None
        if moved is None:
            raise HiddenPartShown(
                f"cut {index + 1} of {media_id} cannot leave out the part the creator hid"
            )
        out[index] = (media_id, moved[0], moved[1], approved)
    return out


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
            # explicit capture-time ask in the brief overrides it. That includes a still-open
            # "random order" ask: the hand reorder is the later word, and the receipt says so.
            return _creator_ordered(clips, creator_order), "creator_order", []
        if view.random_order_seed is not None:
            return seeded_permutation(clips, view.random_order_seed), "random", []
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


def _group_landed(
    position: str | None,
    index: int,
    mine: Sequence[str],
    spot: Mapping[str, int],
    owner: Mapping[str, int],
    rows: Sequence[tuple[str | None, str, list[str], str]],
) -> bool:
    """KRI-941: is row ``index``'s group ``mine`` where its ``position`` put it in the cut?

    ``spot`` is each clip's place in the finished cut and ``owner`` the row that seats it. A
    ``first`` group must play before every clip outside it, bar the clips an earlier ``first``
    row leads with; a ``last`` group after every clip outside it, bar the clips a later
    ``last`` row closes with. A ``then`` group must play after every clip a ``first`` row
    seats (listed anywhere) and every clip an earlier ``then`` row seats, and before every
    clip a ``last`` row seats. Clips no row names are not judged against a ``then`` group.
    """
    here = [spot[m] for m in mine]
    if position == "first":
        led = {m for m, at in owner.items() if at < index and rows[at][0] == "first"}
        others = [i for m, i in spot.items() if m not in mine and m not in led]
        return max(here) < min(others, default=len(spot))
    if position == "last":
        closed = {m for m, at in owner.items() if at > index and rows[at][0] == "last"}
        others = [i for m, i in spot.items() if m not in mine and m not in closed]
        return min(here) > max(others, default=-1)
    before = [
        spot[m]
        for m, at in owner.items()
        if rows[at][0] == "first" or (rows[at][0] is None and at < index)
    ]
    after = [spot[m] for m, at in owner.items() if rows[at][0] == "last"]
    return min(here) > max(before, default=-1) and max(here) < min(after, default=len(spot))


def _sequence_outcomes(
    rows: Sequence[tuple[str | None, str, list[str], str]],
    ordered: Sequence[UnifiedClip],
    *,
    seats: MomentSeats | None = None,
    cuts: Sequence[tuple[str, float]] = (),
    spans: Sequence[tuple[str, float, float]] = (),
    moment_record: Sequence[Mapping[str, Any]] | None = None,
    unconfirmed_groups: frozenset[SequenceGroupKey] = frozenset(),
    seated_visuals: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Did each described group land where the creator said? Read off the finished order.

    Every row also carries a ``code`` (KRI-503) so the receipt never re-parses ``reason``:
    ``landed`` / ``misplaced`` (a described group is not where it was asked), ``absent`` (none
    of its clips are in the edit), ``contained`` (a ``then`` group whose clips other rows
    all seat: an earlier group, or a first / last pin, KRI-1109), ``repeat`` (a first / last
    group naming clips an earlier group already seated at that same spot: the creator said it
    twice, KRI-764), ``conflict`` (a first / last group naming clips an earlier pin seated at
    the other end: one clip cannot be both, the earlier claim stands), ``visual`` (its
    members are Visuals-pool items, which no sequence rule seats) and ``unresolved``.
    ``status`` keeps its meaning for every other consumer.

    A row lands (KRI-941) when the finished cut agrees with it against EVERY other clip in the
    edit, named or not: a ``first`` group leads the whole cut, a ``last`` group closes it, and
    a ``then`` group plays after the clips ``first`` rows seat and after the clips of the
    ``then`` rows listed before it, and before the clips ``last`` rows seat. Rows are in the
    creator's listed order; clips no row names may sit anywhere between the groups. Explicit
    lists expand into ordered rows. Old multi-member rows without ordering certainty cannot
    claim their internal order was checked (``test_kri941_listed_order_replay``).

    ``seats`` (KRI-796) are the rows read as moments of one take (``moment_seats``) and
    ``cuts`` the finished cut order as (clip ref, source start): those rows are judged off
    the cuts by ``_moment_outcomes`` and carry the ``moment`` they name or follow.

    ``seated_visuals`` (KRI-1069) are the refs of the dated Visuals a filming-order montage
    seats in the chronology like clips ("open on the lineup sheet" names a photo): they are
    judged like clips. Empty (the default), every Visual is left out as before.
    """
    seated = [c for c in ordered if c.lane == "clip" or c.ref_id in seated_visuals]
    spot = {clip.ref_id: i for i, clip in enumerate(seated)}
    visuals = {clip.ref_id for clip in ordered if clip.lane != "clip"} - seated_visuals
    # The row that seats each clip, by ``apply_sequence``'s own rule: a first / last row
    # takes its clips out of a ``then`` group (KRI-1109), else the first row naming them.
    owner = seat_owners(rows, present=spot)
    relations = row_relations(rows, present=spot)
    moments = (
        _moment_outcomes(seats, cuts, spans=spans, record=moment_record)
        if seats is not None
        else {}
    )
    out: list[dict[str, Any]] = []
    landed_clips: set[str] = set()
    for index, ((position, name, members, status), relation) in enumerate(
        zip(rows, relations, strict=True)
    ):
        label = f"{position or 'then'}: {name}" if name else "the order you described"
        row = {"op": "order", "name": label, "position": position or "then"}
        if index in moments:
            out.append({**row, **moments[index]})
            continue
        mine = [m for m in members if owner.get(m) == index]
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
            if any(m in owner for m in members):
                code, reason = "contained", "its clips are already in an earlier group"
            elif members and all(m in visuals for m in members):
                code, reason = "visual", "I found no clips of it"
            else:
                code, reason = "absent", "I found no clips of it"
            out.append({**row, "status": "not_possible", "code": code, "reason": reason})
            continue
        met = _group_landed(position, index, mine, spot, owner, rows)
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
    for (position, name, members, _status), result in zip(rows, out, strict=True):
        if (position, name, tuple(members)) in unconfirmed_groups and result["status"] == "met":
            result.update(
                status="partial", code="member_order_unknown", reason=UNCONFIRMED_MEMBER_ORDER
            )
    return out


# KRI-723: what a named moment's row says when the plan cannot show it is that action. Each
# is a limit the creator already knows about (an app that could not ask, a moment they let
# Kria pick or said is not in their clips), so it never blocks the render
# (``brief_checks.is_format_limit``).
MOMENT_NOT_LOCATED = (
    "I couldn't find this moment in your clip, so I can't confirm it's the one playing"
)
MOMENT_DELEGATED = "you let me pick this moment, so I chose it myself"
MOMENT_SKIPPED = "you told me this moment isn't in your clips"
# How much of the evidenced interval a cut must show to be that moment (KRI-596's share).
_MOMENT_COVER = 0.5


def _pinned_seating(
    record: Sequence[Mapping[str, Any]], skipped: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """``moment_seats`` arguments for an approved plan's named moments (KRI-723)."""
    rows = {int(m["row"]) for m in record if isinstance(m.get("row"), int)}
    return {
        "pinned": [
            (int(m["row"]), str(m.get("words") or ""))
            for m in sorted(record, key=lambda m: int(m.get("window_index", 0)))
            if isinstance(m.get("row"), int)
        ],
        "skipped_rows": {
            int(m["row"]) for m in skipped if isinstance(m.get("row"), int) and m["row"] not in rows
        },
    }


def _pinned_cuts(
    spans: Sequence[tuple[str, float, float, bool]],
    record: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[int, str]]:
    """(cut index, the creator's words) of each moment the CREATOR pointed at (KRI-723): the
    take's cuts play the record's windows in order, so its j-th cut is the j-th moment. A
    grounded moment's freeze keeps KRI-828's analysed-moment frame instead."""
    if not record:
        return []
    rows = sorted(record, key=lambda m: int(m.get("window_index", 0)))
    take = rows[0].get("media_id")
    cuts = [k for k, (media, *_rest) in enumerate(spans) if media == take]
    return [
        (cuts[j], str(row.get("words") or ""))
        for j, row in enumerate(rows)
        if j < len(cuts) and row.get("origin") == "creator_answer"
    ]


def _moment_verdict(
    moment: int,
    take_spans: Sequence[tuple[float, float]],
    record: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, str, str | None]:
    """(status, code, reason) for the ``moment``-th window of the take (1-based).

    Met only when the plan carries evidence for it (the creator's own pointed window or a
    grounded interval) AND the cut in that spot really shows it. Six windows in the right
    count and order are no evidence of six actions (KRI-723's wrong-content control).
    """
    if record is None or moment > len(record) or moment > len(take_spans):
        return "partial", "not_located", MOMENT_NOT_LOCATED
    row = sorted(record, key=lambda m: int(m.get("window_index", 0)))[moment - 1]
    origin = row.get("origin")
    if origin == "ai_pick_by_request":
        return "partial", "delegated", MOMENT_DELEGATED
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    if origin == "grounded":
        start, end = evidence.get("source_start_s"), evidence.get("source_end_s")
    elif origin == "creator_answer":
        start, end = row.get("source_start_s"), row.get("source_end_s")
    else:
        return "partial", "not_located", MOMENT_NOT_LOCATED
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return "partial", "not_located", MOMENT_NOT_LOCATED
    cut_start, cut_end = take_spans[moment - 1]
    shown = min(cut_end, end) - max(cut_start, start)
    if shown + 1e-6 < _MOMENT_COVER * min(cut_end - cut_start, end - start):
        return "partial", "misplaced", "the clips did not end up there"
    return "met", "landed", None


def _moment_outcomes(
    seats: MomentSeats,
    cuts: Sequence[tuple[str, float]],
    *,
    spans: Sequence[tuple[str, float, float]] = (),
    record: Sequence[Mapping[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    """KRI-796 / KRI-723: each row ``moment_seats`` read, judged off the finished cuts.

    A moment is met only when the take plays exactly its windows and the cut in its spot
    shows the evidenced interval (``_moment_verdict``); without evidence it is an honest
    "couldn't find it", never met. A Visual's row is met when every Visual it names plays
    right after its moment (as many cuts of the take before it as the moment's number) AND
    that moment is met. Never read off the seating itself, so a layout that misses the spot
    says so. Explicit first/last rows must additionally occupy the absolute boundary block,
    with multiple boundary groups in their listed order.
    """
    take_spans = [(start, end) for ref, start, end in spans if ref == seats.take]
    if not spans:
        take_spans = [(start, start) for ref, start in cuts if ref == seats.take]
    starts = [start for start, _end in take_spans]
    # Without an approved record the windows play in take order (KRI-596); with one, in the
    # creator's order, which the record's windows already are.
    in_order = len(starts) == seats.moments and (record is not None or starts == sorted(starts))
    verdicts = {
        m: (
            _moment_verdict(m, take_spans, record)
            if in_order
            else ("partial", "misplaced", "the clips did not end up there")
        )
        for m in range(1, seats.moments + 1)
    }

    def row(moment: int | None, verdict: tuple[str, str, str | None]) -> dict[str, Any]:
        status, code, reason = verdict
        return {"status": status, "code": code, "reason": reason, "moment": moment}

    def worst(moments: Sequence[int]) -> tuple[str, str, str | None]:
        missed = [verdicts[m] for m in moments if verdicts[m][0] != "met"]
        misplaced = [v for v in missed if v[1] == "misplaced"]
        return (misplaced or missed or [("met", "landed", None)])[0]

    def boundary_seated(position: str) -> bool:
        groups: list[set[str]] = []
        claimed: set[str] = set()
        for index, (_moment, members) in seats.placed.items():
            if seats.positions.get(index) != position:
                continue
            fresh = set(members) - claimed
            if fresh:
                groups.append(fresh)
                claimed.update(fresh)
        if not claimed:
            return False
        refs = [ref for ref, _start in cuts]
        edge = refs[: len(claimed)] if position == "first" else refs[-len(claimed) :]
        if set(edge) != claimed:
            return False
        previous = -1
        for members in groups:
            spots = [i for i, ref in enumerate(refs) if ref in members]
            if not spots or min(spots) <= previous:
                return False
            previous = max(spots)
        return True

    out: dict[int, dict[str, Any]] = {}
    for index, moment in seats.named.items():
        out[index] = row(moment, verdicts[moment])
    for index, moments in seats.listed.items():
        out[index] = row(None, worst(moments))
    for index in seats.skipped:
        out[index] = row(None, ("partial", "skipped", MOMENT_SKIPPED))
    for index in seats.conflicted:
        out[index] = row(
            None, ("partial", "misplaced", "the clip's position conflicts with its named moment")
        )
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
        seated = in_order and all(
            sum(ref == seats.take for ref, _s in cuts[:spot]) == moment for spot in spots
        )
        if index in seats.positions:
            seated = seated and boundary_seated(seats.positions[index])
        verdict = (
            # An explicit opening Visual has no preceding moment; verify the adjacent first window.
            verdicts[max(1, moment)]
            if seated
            else ("partial", "misplaced", "the clips did not end up there")
        )
        out[index] = row(moment, verdict)
    return out


_Slot = tuple[int, UnifiedClip, int, BestPart | None]


def _seat_among_moments(slots: list[_Slot], seats: MomentSeats) -> list[_Slot]:
    """KRI-796: a seated Visual's cut plays right after its moment, the take's n-th window.

    ``slots`` are the cuts to emit as (index into ``ordered``, clip, frames, window), in
    the order the clips were laid out. Explicit first/last groups surround all remaining
    slots; non-boundary cuts keep their relative order.
    """
    claimed: set[str] = set()

    def boundary_slots(position: str) -> list[_Slot]:
        placed: list[_Slot] = []
        for index, (_moment, members) in seats.placed.items():
            if seats.positions.get(index) != position:
                continue
            fresh = set(members) - claimed
            placed.extend(slot for slot in slots if slot[1].ref_id in fresh)
            claimed.update(fresh)
        return placed

    leading = boundary_slots("first")
    trailing = boundary_slots("last")
    moved = [
        slot for slot in slots if slot[1].ref_id in seats.after and slot[1].ref_id not in claimed
    ]
    out: list[_Slot] = list(leading)
    played = 0
    for slot in slots:
        if slot[1].ref_id in claimed or slot[1].ref_id in seats.after:
            continue
        out.append(slot)
        if slot[1].ref_id == seats.take:
            played += 1
            out.extend(m for m in moved if seats.after[m[1].ref_id] == played)
    out.extend(trailing)
    return out if len(out) == len(slots) else slots


def _ordering_choice(strategy: Mapping[str, Any], enabled: bool) -> str | None:
    choice = strategy.get("ordering_choice") if enabled else None
    return choice if choice in ("group_first", "chronological", "random") else None


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
    output_orientation_reason: str | None = None,
    creator_duration_proven: bool = False,
    approved_length_plan: Mapping[str, Any] | None = None,
    speech_order: bool = False,
    freeze_frames: bool = False,
    clip_speed: bool = False,
    text_bend: bool = False,
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
    Visuals-pool items (``lane="asset"``) in upload order; see ``_scatter``. Under a
    filming-order ask, dated ones are ordered with the clips by capture time (KRI-1069).
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
    (``freezeFrame`` verified). Then each of the creator's freeze asks (``view.freezes``,
    KRI-899) is grounded to its own cut (see ``_ground_freezes``): that cut's window ends on
    the frozen frame and holds it. Off, or with no freeze ask, the plan is byte-identical.

    ``clip_speed`` (KRI-689) says the renderer can play a cut faster or slower
    (``variableSpeed`` verified). Then each of the creator's speed asks (``view.speeds``) is
    grounded to its cuts (see ``_ground_speeds``) and those cuts carry ``playback_rate``: a
    planned cut keeps its OUTPUT length and shows ``rate`` times as much footage, an approved
    window keeps its exact source edges and its output length follows the rate. Off, the asks
    are recorded as locked so the receipt says why. With no speed ask, the plan is
    byte-identical.

    ``text_bend`` (KRI-1029) says the phone that approved can draw bent text
    (``phone_rollout.text_bend_planned_for``). Then every text the creator asked curved
    (``view.curved_*``) carries ``CURVED_TEXT_BEND``; off, it stays straight and the record's
    ``text_shapes`` says why. With no curve ask the plan is byte-identical.

    KRI-1030: the opening title is never drawn on top of the first clip's own label with the
    same creator words (a re-draft's ``opening_title`` beside the brief's "title on the palm
    trees"); the clip's label stays, where the creator put it, and covers the opening.
    """
    view = view or BriefView()
    if view.kept_filming_order and not view.order_by_capture and not creator_order:
        # Leverkusen M1 run 2 #3: "Make it without these" dropped the filming-order ask
        # (its check failed), and this plan fell back to upload order: the train ride,
        # filmed second, played 10th of 11. Plan exactly as under the ask (dated photos
        # join the chronology, KRI-1069); with no live requirement, no receipt or contract
        # promises it. A hand reorder made since is the later word and keeps its order.
        view = replace(view, order_by_capture=True)
    strategy = _without_withdrawn_texts(strategy or {}, view.withdrawn_texts)
    creator_omissions = creator_omission_outcomes(strategy)
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
    # The planner's pace sets how long a clip is held when nothing else does (a creator
    # length, a label's reading time and an approved window all still win). None keeps the
    # constants below byte for byte (silence, or ``PACE_DRIVES_CUTS_ENABLED`` off).
    pace = effective_pace(strategy)
    default_cut_s = pace_hold_bounds(pace).preferred_s if pace is not None else DEFAULT_CUT_S
    growth_max_s = pace_hold_bounds(pace).maximum_s if pace is not None else READING_MAX_S
    # A story direction (text_explainer / guided_story) holds every clip at least its own
    # minimum moment: the same floor approval-time planning applies, only where nothing the
    # creator asked keeps this a fast montage (`story_direction.story_blocker`).
    story_floor_s = story_floor_for(strategy, view) if song_duration_s is None else None
    if song_duration_s is not None and (song_plan_item_id is None or song_generation is None):
        raise ValueError("a song montage needs the song's plan item and generation")
    if not clips:
        raise ValueError("a montage needs at least one clip")
    if any(visual.lane != "asset" for visual in visuals):
        raise ValueError("montage visuals must come from the Visuals pool")
    if len({clip.media_id for clip in (*clips, *visuals)}) != len(clips) + len(visuals):
        raise ValueError("montage clips must have unique media identities")
    # KRI-1069: under a filming-order ask a dated Visual is footage like any clip: it joins
    # the chronology by its own capture time, before the creator's sequence ("open on the
    # lineup sheet") seats anything, exactly as the render contract pins it. Undated
    # Visuals keep their spread slot below.
    timed_visuals = (
        [visual for visual in visuals if visual.capture_time is not None]
        if view.order_by_capture
        else []
    )
    ordered, basis, fallback_ids = _order([*clips, *timed_visuals], view, creator_order)
    if timed_visuals and basis != "capture_time":
        # Fewer than two dated items: nothing to sort, so the Visuals spread as before.
        timed_visuals = []
        ordered, basis, fallback_ids = _order(clips, view, creator_order)
    timed_ids = {visual.media_id for visual in timed_visuals}
    spread_visuals = [visual for visual in visuals if visual.media_id not in timed_ids]
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
    ordered = _scatter(ordered, spread_visuals)
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
        # Visuals that did not join the chronology keep their spread slot, and the
        # receipt says their place is not the filmed order.
        fallback_ids = [*fallback_ids, *(v.media_id for v in spread_visuals)]

    # Leverkusen M2: parts of a clip the creator asked never to show. Every window below is
    # kept out of them, and a privacy ask nobody settled (no clip, edge or seconds) is never
    # rendered around: the plan is refused.
    unsettled = unsettled_hidden_parts(strategy)
    if unsettled:
        raise HiddenPartShown(f"which part of {unsettled[0]} to leave out is not settled")
    hidden = _hidden_by_media(ordered, hidden_parts_from_strategy(strategy))
    clip_durations = {clip.media_id: float(clip.duration_s) for clip in ordered}

    # KRI-496: only footage-owned, unnamed timing gets its length and source
    # windows from analysis. Song, voiceover, original-audio takes and a proven
    # creator duration keep their dedicated policies below. Images retain the
    # established Visuals hold behavior because they have no source window.
    best_parts: BestPartsResult | None = None
    best_parts_conflicts: list[dict[str, Any]] = []
    has_approved_windows = False
    # KRI-723: the approved plan's named moments (envelope v2), each with its authority.
    moment_record: list[Mapping[str, Any]] | None = None
    skipped_record: list[Mapping[str, Any]] = []
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
        if isinstance(approved_length_plan.get("moments"), list):
            moment_record = [m for m in approved_length_plan["moments"] if isinstance(m, Mapping)]
            skipped_record = [
                m
                for m in approved_length_plan.get("skipped_moments") or []
                if isinstance(m, Mapping)
            ]
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
    title, title_source = _title(
        strategy,
        view,
        [label.text for label in labels.values()],
        [label.text for label in labels.values() if label.provenance == "creator"],
    )
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
        for media_id, parts in hidden.items():
            # A hidden part's clip offers only what it may still show.
            room = usable_s(clip_durations[media_id], parts)
            seat_maximums[media_id] = min(seat_maximums.get(media_id, room), room)
        candidate = select_best_parts(
            sources,
            minimum_total_duration_s=MIN_TOTAL_S,
            minimum_duration_s=minimums,
            maximum_duration_s=seat_maximums,
        )
        if pace is not None or story_floor_s is not None:
            # The same rule the approval-time plan applies (``paced_selection``): the planner's
            # pace (and a story direction's minimum moment) sets how long each clip is held,
            # with no creator length stated.
            from app.services.length_planning import paced_selection  # noqa: PLC0415

            candidate = (
                paced_selection(
                    sources,
                    pace,
                    budget=None,
                    extend_budget=False,
                    cap_s=MAX_BEST_PARTS_DURATION_S,
                    minimums=minimums,
                    maximums=seat_maximums,
                    story_floor_s=story_floor_s,
                )
                or candidate
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
    # KRI-824: a photo the creator gave its own seconds plays exactly that, whatever the
    # AI's photo pacing; only the item's own photos resolve, by brief scope id.
    still_media = {clip.ref_id: clip.media_id for clip in ordered if clip.kind == "image"}
    # A media id its own ref id already ends with ("asset-<uuid>") resolves through the ref;
    # listing both would make every tail of that one photo look ambiguous.
    still_media.update(
        {
            clip.media_id: clip.media_id
            for clip in ordered
            if clip.kind == "image" and not clip.ref_id.endswith(clip.media_id)
        }
    )
    holds: dict[str, AssetHold] = {
        still_media[ref]: hold
        for ref, hold in holds_from_requirements(view.hold_requirements, still_media).items()
    }
    # KRI-825: a length the creator stated for the photos as a group ("every photo 1
    # second", read from their own words into the profile) holds every other photo exactly
    # the same way; a photo with its own stated length keeps that.
    for media_id in still_media.values():
        group = group_still_hold(media_id, mixed_media_timing)
        if group is not None and media_id not in holds:
            holds[media_id] = group
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
            **(_pinned_seating(moment_record, skipped_record) if moment_record is not None else {}),
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
        elif clip.media_id in holds:
            want_s = holds[clip.media_id].hold_s
        elif clip.kind == "image" and (mixed_media_timing is not None or label is None):
            want_s = unheld_still_frames(mixed_media_timing) / FPS
        else:
            want_s = label.min_display_s if label is not None else default_cut_s
        want = int(math.ceil(want_s * FPS - 1e-9))
        if clip.media_id in holds and not approved_parts:
            want = holds[clip.media_id].frames
        if approved_parts or clip.media_id in holds:
            cap = want
        elif clip.kind == "image" and mixed_media_timing is not None:
            # The snapshot validator owns these photo bounds too.  Keep the
            # legacy minimum-duration grower from stretching a profiled still
            # past its approved maximum.
            image_bounds = mixed_media_hold_bounds("image", mixed_media_timing)
            cap = min(_capacity_frames(clip), int(math.floor(image_bounds.maximum_s * FPS)))
        else:
            cap = _capacity_frames(clip, hidden.get(clip.media_id, ()))
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
        list(capacity) if target_s else [min(cap, int(growth_max_s * FPS)) for cap in capacity]
    )
    # A held photo is pinned like a labelled cut: never grown, never shrunk (``_shrink``
    # only takes frames from unlabelled cuts), never trimmed for a song below.
    pinned = [clip.media_id in holds for clip in ordered]
    for index, held in enumerate(pinned):
        if held:
            growth_ceiling[index] = wanted[index]
            labelled[index] = True
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
            # The last cut the AI sized gives way; a creator-held photo never does.
            last = max((i for i, held in enumerate(pinned) if not held), default=None)
            if last is None:
                raise ValueError("the song is too short for the photo lengths you asked for")
            if wanted[last] - excess >= int(math.ceil(MIN_VIDEO_CUT_S * FPS)) or len(ordered) == 1:
                wanted[last] = max(1, wanted[last] - excess)
                total = sum(wanted)
                break
            gone = ordered.pop(last)
            total -= wanted.pop(last)
            capacity.pop(last)
            labelled.pop(last)
            growth_ceiling.pop(last)
            pinned.pop(last)
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
            if clip.media_id in holds:
                # A creator-held photo keeps its length; the beat snap moves the others.
                lo_ms.append(nominal)
                hi_ms.append(nominal)
                continue
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
    # Leverkusen M2: a selector's or a default window slides out of a hidden part (same
    # length); a creator-approved window never moves, so one that shows a part is refused.
    spans = _kept_out(spans, hidden, clip_durations, movable=lambda _index: not approved_windows)
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
    freeze_holds: dict[int, float] = {}
    freeze_records: list[dict[str, Any]] = []
    if freeze_frames and view.freezes and song_duration_s is None:
        spans, freeze_holds, freeze_records = _ground_freezes(
            view.freezes,
            spans,
            ordered,
            answers=freeze_frame_answers(strategy),
            pinned=_pinned_cuts(spans, moment_record),
        )
        if freeze_holds and target_s:
            # A creator-stated length already sized the cuts: the hold comes out of it.
            shrinkable = [
                not approved
                and not labelled[slots[k][0]]
                and slots[k][1].kind == "video"
                and k not in freeze_holds
                for k, (_media, _start, _end, approved) in enumerate(spans)
            ]
            spans, reserved = _reserve_freeze_hold(
                spans, freeze_holds, shrinkable, float(target_s), {}
            )
            if "length_over_s" in reserved:
                freeze_records = [
                    {**row, "length_over_s": reserved["length_over_s"]}
                    if row.get("status") == "held"
                    else row
                    for row in freeze_records
                ]
    # KRI-689: the creator's speed asks, each grounded to its cuts. Under a song the cuts
    # sit on the song's beats and its lyric captions, so nothing is retimed there (locked).
    rates: dict[int, float] = {}
    speed_records: list[dict[str, Any]] = []
    if view.speeds:
        if song_duration_s is not None:
            speed_records = [_locked_speed(spec, "user_song") for spec in view.speeds]
        elif not clip_speed:
            speed_records = [_locked_speed(spec, "renderer") for spec in view.speeds]
        else:
            spans, rates, speed_records = _ground_speeds(
                view.speeds,
                spans,
                ordered,
                pinned=_pinned_cuts(spans, moment_record),
                freeze_holds=freeze_holds,
                # Only a creator-approved window keeps its edges; a selector's pick is a
                # planned cut whose output length matters more than its exact footage.
                creator_windows=approved_windows,
            )
    if hidden:
        # A freeze ends on its creator's frame and a retimed cut shows more or less footage:
        # both can reach a hidden part again. A held frame never moves.
        spans = _kept_out(
            spans,
            hidden,
            clip_durations,
            movable=lambda index: not approved_windows and index not in freeze_holds,
        )
    cuts = [
        FastMontageCut(
            cut_id=f"unified-cut-{index + 1}",
            media_id=media_id,
            source_start_s=start,
            source_end_s=end,
            output_duration_s=(
                (
                    ((end - start) if approved else round(end - start, 3))
                    if index not in rates
                    else round((end - start) / rates[index], 3)
                )
                + freeze_holds.get(index, 0.0)
            ),
            role="hook" if index == 0 else "payoff" if index == len(spans) - 1 else "build",
            transition="none",
            beat_align=False,
            **({"freeze_hold_s": freeze_holds[index]} if index in freeze_holds else {}),
            **({"playback_rate": rates[index]} if index in rates else {}),
        )
        for index, (media_id, start, end, approved) in enumerate(spans)
    ]
    # The last word before any compiler sees the plan: no cut shows a hidden part.
    check_windows(
        [(cut.media_id, cut.source_start_s, cut.source_end_s) for cut in cuts],
        clip_durations,
        [part for parts in hidden.values() for part in parts],
    )
    for row in freeze_records:
        if row.get("status") == "held":
            row["cut_id"] = cuts[row.pop("cut_index")].cut_id
    for row in speed_records:
        indexes = row.pop("cut_indexes", [])
        row["cut_ids"] = [cuts[k].cut_id for k in indexes]
        row["cut_positions"] = [k + 1 for k in indexes]
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
    # KRI-522: the brief is cumulative, so the creator's ask survives a later turn whose
    # strategy omits it; the strategy's own typed value is the fallback.
    title_animation = (
        view.title_animation
        or normalize_title_animation(strategy.get("title_animation"))
        # A named edge for "all the text" is the title's too (the title is drawn here, so it
        # keeps its own centred, held look); an unnamed ask is completed by the text pass.
        or view.text_animation
    )
    title_shown_as_label = _title_shown_as_label(
        title, labels.get(cuts[0].media_id) if cuts else None, strategy, title_animation
    )
    if title_shown_as_label is not None:
        title, title_source = None, "none"
    labels, title_bend, text_shapes = _curved_texts(
        view, title, labels, title_animation, phone_bends=text_bend
    )

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
    if holds:
        snapshot_kwargs["creator_still_holds"] = [
            CreatorStillHold(
                media_id=media_id, hold_s=hold.frames / FPS, requirement_id=hold.requirement_id
            )
            for media_id, hold in holds.items()
            if any(cut.media_id == media_id for cut in cuts)
        ]
    if title_animation is not None and title:
        snapshot_kwargs["title_animation"] = title_animation
    if title_bend is not None:
        snapshot_kwargs["title_bend"] = title_bend
    label_position = (
        view.label_position
        # A global "bottom left" is the corner the labels this route draws sit in.
        or view.text_position
        or normalize_label_position(strategy.get("label_position"))
    )
    if label_position is not None and labels:
        snapshot_kwargs["label_position"] = label_position
    if output_orientation in ("portrait", "landscape"):
        snapshot_kwargs["output_orientation"] = output_orientation
        # The caller says whether the creator picked this shape or the app echoed the
        # server default; absent keeps the original wording.
        snapshot_kwargs["output_orientation_reason"] = (
            output_orientation_reason or CREATOR_SELECTED_ORIENTATION_REASON
        )
    style: dict[str, Any] = {}
    if family is not None:
        style["font_family"] = family
    if isinstance(strategy.get("text_color"), str) and strategy.get("text_color"):
        style["text_color"] = strategy["text_color"]

    # The strategy's direction where this route can build it; else fast_montage, and the plan
    # records what was asked (the approval card's disclosure reads it).
    built_direction = route_direction(strategy)

    def build(extra: Mapping[str, Any]) -> EditProposalSnapshot:
        return EditProposalSnapshot(
            direction=built_direction,  # type: ignore[arg-type]
            goal=(editorial.get("viewer_goal", "") if editorial else ""),
            editorial_plan=editorial,
            pace=stated_pace(strategy) or "fast",
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
    plan = UnifiedMontagePlan(
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
            spans=[(ref_of[cut.media_id], cut.source_start_s, cut.source_end_s) for cut in cuts],
            moment_record=moment_record,
            seated_visuals=frozenset(v.ref_id for v in timed_visuals),
        )
        + creator_omissions,
        ordering_choice=choice,
        random_order_seed=view.random_order_seed if basis == "random" else None,
        random_order_source_ids=[clip.media_id for clip in clips] if basis == "random" else [],
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
        freeze_frames=freeze_records,
        clip_speeds=speed_records,
        asset_holds=hold_outcomes(
            holds,
            {
                media_id: sum(cut.output_duration_s for cut in cuts if cut.media_id == media_id)
                for media_id in holds
                if any(cut.media_id == media_id for cut in cuts)
            },
        ),
        title_shown_as_label=title_shown_as_label,
        text_shapes=text_shapes,
        requested_direction=direction_not_built(strategy),
        hidden_parts=[
            {
                **part.json(),
                "shown_from_s": min(c.source_start_s for c in cuts if c.media_id == media_id),
                "shown_to_s": max(c.source_end_s for c in cuts if c.media_id == media_id),
            }
            for media_id, parts in hidden.items()
            for part in parts
            if any(cut.media_id == media_id for cut in cuts)
        ],
    )
    return _as_story_direction(
        plan,
        strategy,
        view,
        approved_length_plan=approved_length_plan,
        song=song_duration_s is not None,
    )


def _as_story_direction(
    plan: UnifiedMontagePlan,
    strategy: Mapping[str, Any],
    view: BriefView,
    *,
    approved_length_plan: Mapping[str, Any] | None,
    song: bool,
) -> UnifiedMontagePlan:
    """``plan`` rebuilt as the planner's story direction where the route can, else ``plan``.

    A text_explainer / guided_story strategy is built as story beats over the same clips
    (`story_direction.build_story_plan`); where a creator's ask or the footage keeps it a fast
    montage the plan says why (``direction_fallback``). Any other direction, or the kill switch
    off, returns ``plan`` untouched.
    """
    direction = story_direction(strategy)
    if direction is None:
        return plan
    from app.pipeline.story_direction import build_story_plan  # noqa: PLC0415

    story, reason = build_story_plan(
        plan,
        strategy=strategy,
        view=view,
        direction=direction,
        stated_pace=stated_pace(strategy),
        approved_length_plan=approved_length_plan,
        song=song,
    )
    if story is not None:
        return story
    return replace(plan, direction_fallback=reason)


def _title_shown_as_label(
    title: str | None,
    first_label: ClipLabel | None,
    strategy: Mapping[str, Any],
    title_animation: str | None,
) -> str | None:
    """The first clip whose own label already says the opening title (KRI-1030), else None.

    Plaza de Mayo M1: the re-draft confirmed ``opening_title: "Plaza de Mayo"`` while the
    brief kept "title on the palm trees" ("Plaza de Mayo", ``clip:<palms>``), so the palms
    clip opened with the words twice (centred at the top 0-2.2 s, and its label at the
    bottom 0-2.7 s). The label is the creator's own placement and spans the whole first
    cut from 0 s, so it is also the opening text: the contract's ``opening`` role and the
    phone repair (`phone_plan_repair._shows`) accept it.

    The title stays when it promises something the label can't keep: a typed hold
    (``opening_title_duration_s``, longer than the cut may be) or an entrance animation.
    A guessed label (place facts) is never what the title is folded into.
    """
    if (
        not title
        or first_label is None
        or first_label.provenance != "creator"
        or fold_text(first_label.text) != fold_text(title)
        or title_animation is not None
        or strategy.get("opening_title_duration_s") is not None
    ):
        return None
    return first_label.media_id


def _curved_texts(
    view: BriefView,
    title: str | None,
    labels: dict[str, ClipLabel],
    title_animation: str | None,
    *,
    phone_bends: bool,
) -> tuple[dict[str, ClipLabel], float | None, list[dict[str, Any]]]:
    """Bend the texts the creator asked curved (KRI-1029): ``(labels, title_bend, rows)``.

    A text is curved when its words are a curved literal of the brief, or when it is the
    title / a label and the brief asks every title / label curved. ``phone_bends`` False
    (the approving build or the server can't draw ``bend``) keeps it straight; so does a
    title entrance that can't be bent (typewriter, `portable_text_layout._bend_supported`),
    which the phone would otherwise straighten silently. ``rows`` records each outcome for
    the receipt; it is empty when nothing drawn was asked curved.
    """
    from app.pipeline.portable_text_layout import _bend_supported  # noqa: PLC0415

    def wanted(text: str, everywhere: bool) -> bool:
        return everywhere or fold_text(text) in view.curved_texts

    rows: list[dict[str, Any]] = []
    title_bend: float | None = None
    if title and wanted(title, view.curved_title):
        bendable = _bend_supported("static", {"entrance": title_animation, "exit": None})
        reason = None if phone_bends and bendable else "phone" if not phone_bends else "animation"
        title_bend = CURVED_TEXT_BEND if reason is None else None
        rows.append(
            {"text": title, "role": "title", "media_id": None, "bend": title_bend, "reason": reason}
        )
    bent: dict[str, ClipLabel] = {}
    for media_id, label in labels.items():
        if not wanted(label.text, view.curved_labels):
            bent[media_id] = label
            continue
        bend = CURVED_TEXT_BEND if phone_bends else None
        bent[media_id] = label.model_copy(update={"bend": bend}) if bend is not None else label
        rows.append(
            {
                "text": label.text,
                "role": "label",
                "media_id": media_id,
                "bend": bend,
                "reason": None if bend is not None else "phone",
            }
        )
    return bent, title_bend, rows


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
# KRI-828: the least motion a held cut keeps before its frozen frame.
_FREEZE_MIN_MOTION_S = 0.3
# KRI-900: a frame the creator picked (`choice_questions._freeze_frame`) must sit at least
# this far before its clip's end to be held.
_FREEZE_END_MARGIN_S = 0.05


def freeze_frame_answers(strategy: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """KRI-900: the creator's answers to "which frame should hold?" on the approved strategy.

    Server-owned (`choice_questions.resolve_choices` writes ``choice_answers`` from the
    thread's own answer events; model output never sets them).
    """
    rows = (strategy or {}).get("choice_answers")
    return [
        row
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, Mapping) and row.get("kind") == CONFLICT_FREEZE_FRAME
    ]


def _speed_row(spec: SpeedSpec) -> dict[str, Any]:
    return {
        "requirement_id": spec.requirement_id,
        "rate": spec.rate,
        "rate_requested": spec.rate_requested,
        "rate_stated": spec.rate_stated,
    }


def _locked_speed(spec: SpeedSpec, reason: str) -> dict[str, Any]:
    """KRI-689: a speed ask no cut of this plan may carry (``reason``: "user_song" when the
    cuts sit on a song's beats and captions, "renderer" when ``variableSpeed`` is not verified,
    "frozen" when every named cut holds a freeze frame)."""
    return {**_speed_row(spec), "status": "locked", "reason": reason, "cut_indexes": []}


def _speed_targets(
    spec: SpeedSpec,
    spans: Sequence[tuple[str, float, float, bool]],
    by_id: Mapping[str, UnifiedClip],
    pinned: Sequence[tuple[int, str]],
) -> tuple[list[int] | None, str]:
    """(cut indexes the ask names, how they were found), or ``(None, "")`` when the creator's
    words name no clip of this edit (or more than one): nothing is guessed (KRI-900's rule).

    In order: a ``clip:<id>`` scope; the creator's words against the moments they pointed at
    before approval (KRI-723, same matcher as a freeze); those words against the clips'
    analysed moments (exactly one clip); a cut position ("the last clip"); nothing named =
    every cut (a whole-edit ask).
    """
    if spec.media_id is not None:
        ref_to_media = {clip.ref_id: clip.media_id for clip in by_id.values()}
        media_id = ref_to_media.get(spec.media_id, spec.media_id)
        hits = [k for k, (media, *_rest) in enumerate(spans) if media == media_id]
        return (hits or None), "scope"
    if spec.target_words:
        from app.services.named_moments import pointed_match  # noqa: PLC0415

        hit = (
            pointed_match(spec.target_words, [words for _index, words in pinned])
            if pinned
            else None
        )
        if hit is not None and 0 <= pinned[hit][0] < len(spans):
            return [pinned[hit][0]], "named_moment"
        in_edit = [
            media_id
            for media_id in dict.fromkeys(media for media, *_rest in spans)
            if media_id in by_id and by_id[media_id].kind == "video"
        ]
        matches = match_moments(
            spec.target_words, [(media_id, by_id[media_id].analysis) for media_id in in_edit]
        )
        clips = list(dict.fromkeys(moment.media_id for moment in matches))
        if len(clips) != 1:
            return None, ""
        return [
            k for k, (media, *_rest) in enumerate(spans) if media == clips[0]
        ], "analysed_moment"
    if spec.position is not None:
        if not spans:
            return None, ""
        return [len(spans) - 1 if spec.position == POSITION_LAST else 0], "cut_position"
    return list(range(len(spans))), "all_clips"


def _ground_speeds(
    specs: Sequence[SpeedSpec],
    spans: list[tuple[str, float, float, bool]],
    clips: Sequence[UnifiedClip],
    *,
    pinned: Sequence[tuple[int, str]] = (),
    freeze_holds: Mapping[int, float] | None = None,
    creator_windows: bool = True,
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], list[dict[str, Any]]]:
    """KRI-689: every speed ask the creator made, each on the cuts it names.

    A planned cut keeps its OUTPUT length, so the timeline the labels, the other cuts and
    the total length hang on does not move: its source window becomes ``length x rate``
    seconds anchored on the same start (slid back when it would run past the clip, never
    into another cut of the same take). When the footage cannot cover that much, the rate is
    lowered to what it covers (``footage_limited``; the receipt says "2.5x, not the 4x you
    asked for") rather than the cut shortened. A creator-approved window (``creator_windows``
    and the span's approved flag: a picked moment) keeps its exact edges and its output
    length follows the rate (#1761's anchor). Photos have no speed; a cut holding a freeze
    frame plays at 1x (the compiler's rule). A later ask on the same cut wins and the earlier
    row says so.
    """
    freeze_holds = freeze_holds or {}
    by_id = {clip.media_id: clip for clip in clips}
    spans = [*spans]
    rates: dict[int, float] = {}
    owner: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for spec in specs:
        row = _speed_row(spec)
        if spec.ramp:
            rows.append({**row, "status": "ramp", "cut_indexes": []})
            continue
        targets, grounding = _speed_targets(spec, spans, by_id, pinned)
        if targets is None:
            rows.append({**row, "status": "not_grounded", "cut_indexes": []})
            continue
        videos = [
            k for k in targets if by_id.get(spans[k][0]) and by_id[spans[k][0]].kind == "video"
        ]
        if not videos:
            rows.append({**row, "status": "photo", "grounding": grounding, "cut_indexes": []})
            continue
        movable = [k for k in videos if k not in freeze_holds]
        if not movable:
            rows.append({**_locked_speed(spec, "frozen"), "grounding": grounding})
            continue
        limited = False
        applied: list[int] = []
        no_footage: list[int] = []
        for k in movable:
            media_id, start, end, approved = spans[k]
            rate = spec.rate
            if not (approved and creator_windows):
                # The footage this cut may use: the clip minus the export margin, and never
                # another cut of the same take (overlapping windows are refused downstream).
                clip = by_id[media_id]
                lower = max(
                    [
                        0.0,
                        *(
                            e
                            for j, (m, _s, e, _a) in enumerate(spans)
                            if j != k and m == media_id and e <= start + 1e-6
                        ),
                    ]
                )
                upper = min(
                    [
                        math.floor((clip.duration_s - _FREEZE_END_MARGIN_S) * 1000) / 1000,
                        *(
                            s
                            for j, (m, s, _e, _a) in enumerate(spans)
                            if j != k and m == media_id and s >= end - 1e-6
                        ),
                    ]
                )
                out_s = end - start
                room = round(upper - lower, 3)
                if out_s <= 0 or room < out_s - 1e-6:
                    no_footage.append(k)
                    continue  # not even the planned cut fits: nothing to retime
                if rate > 1 and round(out_s * rate, 3) > room:
                    rate = round(room / out_s, 3)
                    if rate < 1.05:
                        # The cut already shows the take whole: no footage to speed through.
                        no_footage.append(k)
                        continue
                    limited = True
                span = min(round(out_s * rate, 3), room)
                new_start = start if start + span <= upper + 1e-6 else max(lower, upper - span)
                new_start = round(new_start, 3)
                spans[k] = (media_id, new_start, round(new_start + span, 3), approved)
            if k in owner:
                earlier = rows[owner[k]]
                earlier["status"] = "replaced"
                earlier["replaced_by"] = spec.requirement_id
                earlier["rate"] = rate
            rates[k] = rate
            owner[k] = len(rows)
            applied.append(k)
        if not applied:
            rows.append(
                {
                    **row,
                    # Every named cut already shows its take whole: nothing to speed through.
                    "status": "no_footage" if no_footage else "not_grounded",
                    "grounding": grounding,
                    "cut_indexes": [],
                }
            )
            continue
        rows.append(
            {
                **row,
                # The slowest rate the footage allowed, when it allowed less than asked.
                "rate": min(rates[k] for k in applied),
                "status": "applied",
                # "Every video clip" only when every one of them changed; otherwise the
                # receipt names the cuts that did.
                "grounding": (
                    "clips_with_footage" if grounding == "all_clips" and no_footage else grounding
                ),
                "cut_indexes": applied,
                "media_ids": list(dict.fromkeys(spans[k][0] for k in applied)),
                "footage_limited": limited,
            }
        )
    return spans, rates, rows


def _ground_freezes(
    specs: Sequence[FreezeSpec],
    spans: list[tuple[str, float, float, bool]],
    clips: Sequence[UnifiedClip],
    *,
    answers: Sequence[Mapping[str, Any]] = (),
    pinned: Sequence[tuple[int, str]] = (),
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], list[dict[str, Any]]]:
    """KRI-899: every freeze the creator asked for, each on its own cut.

    ``pinned`` (KRI-723): (cut index, the creator's words) for each named moment the approved
    plan located; a freeze naming one holds that cut's own last frame (``_ground_freeze``).

    The latest ask is placed first: when two asks hold the same frame ("freeze Atakan's bite
    for one second", later "freeze the bite for 2 seconds"), the later one holds it and the
    earlier one is answered by it (``requirement_ids``), so a frame is never held twice and
    the latest correction wins. A cut already holding one freeze never takes another.
    """
    holds: dict[int, float] = {}
    rows: list[dict[str, Any]] = []
    claimed: dict[tuple[str, float], dict[str, Any]] = {}
    for spec in reversed(specs):
        spans, held, row = _ground_freeze(
            spec,
            spans,
            clips,
            claimed=claimed,
            taken=set(holds),
            answers=answers,
            pinned=pinned,
        )
        frame = row.pop("_frame", None)
        winner = claimed.get(frame) if frame is not None else None
        if winner is not None:
            winner["requirement_ids"] = [*row["requirement_ids"], *winner["requirement_ids"]]
            continue
        if frame is not None and row.get("status") == "held":
            claimed[frame] = row
        holds.update(held)
        rows.append(row)
    rows.reverse()
    return spans, holds, rows


def _freeze_answer(
    spec: FreezeSpec, answers: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The creator's answer that settles this freeze's frame (latest wins), or None."""
    ids = set(spec.requirement_ids)
    found = None
    for row in answers:
        if ids.intersection(str(rid) for rid in row.get("requirement_ids") or ()):
            found = row
    return found


def _ground_freeze(
    spec: FreezeSpec,
    spans: list[tuple[str, float, float, bool]],
    clips: Sequence[UnifiedClip],
    *,
    claimed: Mapping[tuple[str, float], Any] | None = None,
    taken: set[int] | None = None,
    answers: Sequence[Mapping[str, Any]] = (),
    pinned: Sequence[tuple[int, str]] = (),
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]]:
    """Where the creator's freeze lands: one cut whose window ends on the frozen frame.

    KRI-900: the frame is never guessed. It is, in order:

    * the second the creator typed ("freeze at 58.2 seconds", `_typed_frame`);
    * the frame the creator picked before approval (``answers``, the ``freeze_frame``
      question): the cut of that clip nearest to it is re-anchored to end there (same length
      of motion, never overlapping another cut of the same take) and holds ``spec.hold_s``.
      "No freeze" is ``declined``;
    * the final cut when the creator named it ("freeze the last shot / at the end"): its own
      last frame. "The first frame / at the start" cannot be held (a hold sits where a cut
      ends) and is ``needs_frame`` with its position;
    * otherwise nothing is held. One analysed moment matching the creator's words
      (``analysis.best_moments[].description``) is ``needs_frame`` (the moment is known,
      the instant is not; the receipt asks for the second); no match, or a tie between
      moments, is ``not_grounded``.
    """
    record: dict[str, Any] = {
        "requirement_id": spec.requirement_id,
        "requirement_ids": list(spec.requirement_ids),
        "hold_s": spec.hold_s,
        "hold_stated": spec.hold_stated,
        "hold_basis": spec.hold_basis,
        **(
            {"hold_requested_s": spec.hold_requested_s} if spec.hold_requested_s is not None else {}
        ),
        **({"previous_hold_s": spec.previous_hold_s} if spec.previous_hold_s is not None else {}),
    }
    claimed = claimed or {}
    taken = taken or set()
    by_id = {clip.media_id: clip for clip in clips}
    in_edit = [
        media_id
        for media_id in dict.fromkeys(media for media, *_rest in spans)
        if media_id in by_id and by_id[media_id].kind == "video"
    ]
    if spec.frame_s is not None:
        # The second the creator typed ("freeze at 58.2 seconds") wins over everything else.
        media_id, frame = _typed_frame(spec, spans, by_id, in_edit)
        record = {**record, "frame_requested_s": spec.frame_s, "frame_scope": spec.frame_scope}
        return _hold_creator_frame(
            spans, by_id, media_id, frame, spec, record, claimed, taken, "creator_frame"
        )
    answer = _freeze_answer(spec, answers)
    if answer is not None:
        option = parse_freeze_option(answer.get("option"))
        if option is None:
            return spans, {}, {**record, "status": "not_grounded"}
        basis, digest, frame = option
        if basis == "no_freeze":
            return spans, {}, {**record, "status": "declined"}
        # The picked clip is no longer in the edit: never a hold somewhere else.
        media_id = next((m for m in in_edit if media_key(m) == digest), None)
        if answer.get("source") == "creator_delegated":
            basis = "delegated_frame"
        return _hold_creator_frame(
            spans, by_id, media_id, frame, spec, record, claimed, taken, basis
        )
    # KRI-723: the creator pointed at this moment before approval; its cut's last frame holds.
    on_moment = _freeze_on_pinned_moment(
        spec, spans, by_id, set(spec.target_words), record, taken, pinned
    )
    if on_moment is not None:
        return on_moment
    if spec.position == POSITION_LAST and not spec.target_words:
        return _hold_cut_end(spans, by_id, spec, record, taken)
    if spec.position is not None and not spec.target_words:
        # "Freeze the first frame / at the start": a hold sits where a cut ends, so the
        # opening frame cannot be held, and the end of the first cut is not what was named.
        # Nothing is guessed; the receipt asks for the second (KRI-900).
        return spans, {}, {**record, "status": "needs_frame", "position": spec.position}
    matches = match_moments(
        spec.target_words, [(media_id, by_id[media_id].analysis) for media_id in in_edit]
    )
    if len(matches) != 1:
        return spans, {}, {**record, "status": "not_grounded"}
    moment = matches[0]
    # The moment is known, the instant inside it is not: nothing is guessed (KRI-900).
    return (
        spans,
        {},
        {
            **record,
            "status": "needs_frame",
            "media_id": moment.media_id,
            "moment_source_s": [round(moment.start_s, 3), round(moment.end_s, 3)],
        },
    )


def _typed_frame(
    spec: FreezeSpec,
    spans: Sequence[tuple[str, float, float, bool]],
    by_id: Mapping[str, UnifiedClip],
    in_edit: Sequence[str],
) -> tuple[str | None, float | None]:
    """KRI-900: ``(clip, source second)`` of the frame the creator typed, or ``(None, None)``.

    "2 seconds into the video" is a time of the edit: the cut playing then, and its source
    second. "at 58.2 seconds" is a second of a clip: the clip of the moment the creator
    named, else the cut they named by position, else the only video clip in the edit. Any
    other case names no clip, and nothing is guessed.
    """
    at = float(spec.frame_s or 0.0)
    if spec.frame_scope == "edit":
        elapsed = 0.0
        for media_id, start, end, _approved in spans:
            if elapsed <= at < elapsed + (end - start):
                return media_id, round(start + at - elapsed, 3)
            elapsed += end - start
        return None, None
    clips: list[str] = []
    if spec.target_words:
        moments = match_moments(
            spec.target_words, [(media_id, by_id[media_id].analysis) for media_id in in_edit]
        )
        clips = list(dict.fromkeys(moment.media_id for moment in moments))
    elif spec.position is not None and spans:
        clips = [spans[-1 if spec.position == POSITION_LAST else 0][0]]
    else:
        clips = list(in_edit)
    return (clips[0], round(at, 3)) if len(clips) == 1 else (None, None)


def _hold_creator_frame(
    spans: list[tuple[str, float, float, bool]],
    by_id: Mapping[str, UnifiedClip],
    media_id: str | None,
    frame: float | None,
    spec: FreezeSpec,
    record: dict[str, Any],
    claimed: Mapping[tuple[str, float], Any],
    taken: set[int],
    grounding: str,
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]]:
    """Hold the frame the creator chose (picked before approval or typed), or nothing."""
    clip = by_id.get(media_id) if media_id is not None else None
    if clip is None or clip.kind != "video" or frame is None:
        return spans, {}, {**record, "status": "not_grounded"}
    if not (_FREEZE_MIN_MOTION_S <= frame < clip.duration_s - _FREEZE_END_MARGIN_S):
        return spans, {}, {**record, "status": "not_grounded"}
    if (media_id, frame) in claimed:
        # A later ask already holds this frame: it answers this one too.
        return spans, {}, {**record, "_frame": (media_id, frame)}
    return _hold_at(spans, media_id, frame, spec, record, taken, grounding=grounding)


def _hold_cut_end(
    spans: list[tuple[str, float, float, bool]],
    by_id: Mapping[str, UnifiedClip],
    spec: FreezeSpec,
    record: dict[str, Any],
    taken: set[int],
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]]:
    """KRI-900: "freeze the last shot / at the end": the final cut holds its own last frame,
    which is the video's last frame.

    Nothing moves: the cut's window already ends on the frame it holds. A photo cut has
    nothing to freeze (KRI-824 holds stills), and a cut already holding a freeze takes no
    other: both are ``not_grounded``.
    """
    index = len(spans) - 1
    if not spans:
        return spans, {}, {**record, "status": "not_grounded", "position": spec.position}
    media_id, start, end, _approved = spans[index]
    clip = by_id.get(media_id)
    if clip is None or clip.kind != "video" or index in taken:
        return spans, {}, {**record, "status": "not_grounded", "position": spec.position}
    return (
        spans,
        {index: spec.hold_s},
        {
            **record,
            "status": "held",
            "media_id": media_id,
            "cut_index": index,
            "frame_source_s": round(end, 3),
            "grounding": "cut_position",
            "position": spec.position,
            # A later "the last shot for 2 seconds" folds into this hold (KRI-899).
            "_frame": (media_id, round(end, 3)),
        },
    )


def _hold_at(
    spans: list[tuple[str, float, float, bool]],
    media_id: str,
    frame: float,
    spec: FreezeSpec,
    record: dict[str, Any],
    taken: set[int],
    *,
    grounding: str,
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]]:
    """Re-anchor the cut of ``media_id`` nearest to ``frame`` so it ends there, and hold it."""
    owned = [
        (index, start, end)
        for index, (media, start, end, _approved) in enumerate(spans)
        if media == media_id
    ]
    free = [row for row in owned if row[0] not in taken]
    if not free or frame <= 0:
        return spans, {}, {**record, "status": "not_grounded"}
    index, start, end = min(free, key=lambda row: abs((row[1] + row[2]) / 2 - frame))
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
            "grounding": grounding,
            "_frame": (media_id, frame),
        },
    )


def _freeze_on_pinned_moment(
    spec: FreezeSpec,
    spans: list[tuple[str, float, float, bool]],
    by_id: Mapping[str, UnifiedClip],
    target: set[str],
    record: dict[str, Any],
    taken: set[int],
    pinned: Sequence[tuple[int, str]],
) -> tuple[list[tuple[str, float, float, bool]], dict[int, float], dict[str, Any]] | None:
    """KRI-723: "freeze the frame on Atakan's bite", and the creator pointed at that bite.

    The creator's words for the freeze are matched against the named moments the approved
    plan located (the same scorer as an analysed moment's description). Exactly one match
    holds the LAST frame of that moment's cut: the window stays exactly what the creator
    pointed at, never re-anchored onto a guessed best moment. No match, or two: None, and
    the analysed-moment grounding runs as before.
    """
    from app.services.named_moments import pointed_match  # noqa: PLC0415

    if not pinned or not target:
        return None
    hit = pointed_match(target, [words for _index, words in pinned])
    if hit is None:
        return None
    index = pinned[hit][0]
    if not 0 <= index < len(spans) or index in taken:
        return None
    media_id, start, end, approved = spans[index]
    clip = by_id.get(media_id)
    if clip is None or clip.kind != "video":
        return None
    frame = round(min(end, math.floor((clip.duration_s - 0.05) * 1000) / 1000), 3)
    if frame - start < _FREEZE_MIN_MOTION_S:
        return None
    spans = [*spans]
    spans[index] = (media_id, start, frame, approved)
    return (
        spans,
        {index: spec.hold_s},
        {
            **record,
            "status": "held",
            "media_id": media_id,
            "cut_index": index,
            "frame_source_s": frame,
            "grounding": "named_moment",
            "moment_source_s": [round(start, 3), round(end, 3)],
            "_frame": (media_id, frame),
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

    The phone draws only the recipe's own face or its system color emoji, so a
    character the font lacks (Fraunces has no "→") fails the whole recipe. Color
    emoji (a flag, a ZWJ sequence) are never the bundled face's and never count
    against it (KRI-1024). Unknown environments (no skia) answer True: the
    compile step stays the source of truth.
    """
    text = drop_color_emoji(text)
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
    import regex  # noqa: PLC0415 - Unicode graphemes

    # Whole graphemes: a flag or ZWJ sequence is kept or dropped as one.
    kept = "".join(g for g in regex.findall(r"\X", text) if g.isspace() or covers(family, g))
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
    texts = [
        text
        for text in (title, closing, *(label.text for label in labels.values()), *extra_texts)
        if text
    ]
    # Color emoji are supplied by the system emoji face, not the bundled one, on
    # both branches (KRI-1024: a flag in the creator's title failed the policy
    # branch). Face selection still chooses DM Sans for an arrow beside an emoji.
    font_texts = [drop_color_emoji(text) for text in texts]
    from app.schemas.phone_text_style import CURATED_PHONE_FONTS  # noqa: PLC0415
    from app.services.phone_text_styling import current_phone_text_policy  # noqa: PLC0415

    policy = current_phone_text_policy()
    if policy:
        # Coverage is role-scoped in the shared pass. This early planner adapter
        # must not strip a label because a separately requested title font lacks
        # its glyphs. Use a drawable provisional face for the compiler dry run.
        chosen = next(
            (
                font
                for font in CURATED_PHONE_FONTS
                if all(covers(font, text) for text in font_texts)
            ),
            None,
        )
        if chosen is None:
            from app.pipeline.phone_text_style import no_font_decline

            raise no_font_decline(
                texts,
                covers,
                fallback="The eligible fonts cannot draw all text. Choose another font.",
            )
        return chosen, title, closing, labels
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


def _creator_wording(intent: Mapping[str, Any]) -> list[str]:
    """The creator's own words a clip-text intent would print (empty for a placeholder)."""
    words = [intent.get("creator_text")]
    if intent.get("caption_grounding") == "creator_text":
        words.append(intent.get("caption_text"))
    words.extend(
        row.get("value")
        for row in intent.get("assignments") or []
        if isinstance(row, Mapping) and row.get("grounding") == "creator_text"
    )
    return [text for text in (_nfc(word) for word in words) if text]


def _without_withdrawn_texts(
    strategy: Mapping[str, Any], withdrawn: frozenset[str]
) -> Mapping[str, Any]:
    """``strategy`` minus the clip text the creator has taken back (KRI-1124).

    Leverkusen M4: "Skip that part" and "Make it without these" superseded the brief's
    "Arrival" / "GOAL" / "Full time" texts, but the clip-intent planner reads the whole
    thread every turn and still wrote their label intents (carrying no ``requirement_id``).
    Planned, they became confirmed copy that the creation-text pass, told the brief asks for
    no text, then removed (the render failed) or left on screen. The brief owns what text is
    wanted, so a label or caption whose every creator-worded text is in ``withdrawn`` is
    dropped from both intent lists, here where the plan reads them; the copy lock stays strict.

    A positional ``shot_labels`` list is dropped only when every row is withdrawn: its rows
    sit on clips by index, so removing one would move the rest onto the wrong clips.
    """
    if not withdrawn:
        return strategy
    out = dict(strategy)
    for key in ("clip_intents", "resolved_clip_intents"):
        rows = strategy.get(key)
        if not isinstance(rows, list):
            continue
        kept = [
            row
            for row in rows
            if not (
                isinstance(row, Mapping)
                and row.get("op") in {"label", "caption"}
                and (words := _creator_wording(row))
                and all(_text_key(word) in withdrawn for word in words)
            )
        ]
        if len(kept) != len(rows):
            out[key] = kept
    positional = [text for text in (_nfc(x) for x in strategy.get("shot_labels") or []) if text]
    if positional and all(_text_key(text) in withdrawn for text in positional):
        out["shot_labels"] = None
    return out


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
    spans: Sequence[tuple[str, float, float]] = (),
    moment_record: Sequence[Mapping[str, Any]] | None = None,
    seated_visuals: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """What each requested group / sport label / chapter text did in this plan (KRI-282).

    Computed from the finished plan, so a receipt can only report what rendered:
    ``status`` is ``met`` / ``partial`` / ``not_possible`` like a requirement receipt.
    The person placeholder is left out (its requirement receipt covers it).
    """
    if not enabled:
        return []
    sequence = _sequence_outcomes(
        _sequence_intents(strategy, enabled),
        ordered,
        seats=seats,
        cuts=cuts,
        spans=spans,
        moment_record=moment_record,
        unconfirmed_groups=unconfirmed_order_groups(strategy.get("resolved_clip_intents")),
        seated_visuals=seated_visuals,
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
        elif op == "include" and intent.get("exclusive") and _is_resolved(intent, "include"):
            # KRI-993: "only about X". Met when every played clip shows X or is one the
            # creator placed by another ask (a pinned closing clip).
            row = {"op": "include", "name": f"only about {name}"}
            named = {
                m
                for other in strategy.get("resolved_clip_intents") or []
                if isinstance(other, Mapping)
                and other.get("op") != "exclude"
                and not other.get("exclusive")
                for m in _members(other)
            }
            off = [
                clip.ref_id
                for clip in ordered
                if clip.lane == "clip"
                and clip.kind == "video"
                and clip.ref_id not in _members(intent)
                and clip.ref_id not in named
            ]
            if not members:
                out.append({**row, "status": "not_possible", "reason": "I found no clips of it"})
            elif off:
                reason = "I couldn't confirm every clip in it shows this"
                out.append({**row, "status": "partial", "reason": reason})
            else:
                out.append({**row, "status": "met", "reason": None})
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
    strategy: Mapping[str, Any],
    view: BriefView,
    label_texts: Sequence[str] = (),
    creator_label_texts: Sequence[str] = (),
) -> tuple[str | None, str]:
    """The opening title and where it came from.

    ``label_texts`` are the per-clip labels the montage prints. A brief literal that only
    lists them ("Bölüm başlıkları: Sabah, Üniversite, Öğle arası, Spor, Akşam") asked for
    chapter titles, which the labels already are: it is never also an opening title (KRI-545).

    ``creator_label_texts`` are the labels written in the creator's own words. A global
    literal that IS one of them ('label "Casa Rosada" on the pink building', filed as global
    text and placed by its clip intent) is that label, never also the opening title (KRI-1030,
    Plaza de Mayo M1 first draft: "Casa Rosada" over the palm trees, then on its own clip).
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
    labelled = {fold_text(text) for text in creator_label_texts}
    if titles(view.global_literal) and fold_text(_nfc(view.global_literal)) not in labelled:
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
    strategy = _without_withdrawn_texts(strategy, view.withdrawn_texts)
    intents = _intent_labels(strategy, True).values()
    creator_labels = [
        *view.clip_literals.values(),
        *(_nfc(text) for text in strategy.get("shot_labels") or []),
        *(text for text, creator in intents if creator),
        *_described_shot_labels(strategy, True).values(),
    ]
    labels = [*creator_labels, *(text for text, creator in intents if not creator)]
    return _title(strategy, view, labels, creator_labels)[0] is not None


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
