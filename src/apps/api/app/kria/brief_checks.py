"""Deterministic per-requirement checks and receipt-driven replies (KRI-188).

No model is involved. Each checker compares one Creative Brief requirement with
facts read from the drafted plan (a strategy or an editor payload) and returns a
``RequirementReceipt``. A requirement nothing could judge (no checker, or the
facts to judge it were missing) gets no receipt: it stays ``open`` and the reply
says nothing about it, so the reply never claims what the server did not check
and never labels an unchecked ask "Partly".

Checks implemented: per-clip text coverage, ordering vs the requested key,
duration within +/-10%, literal on-screen text, "keep my whole take" on a
single-clip subtitled edit, and word-triggered pop-ins (reaction beats) plus the
closing shot on a phone Talking edit.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_route import (
    END_KEYS,
    START_KEYS,
    first_text,
    fold_text,
    loose_text,
    wants_filming_time_text,
    wants_hour_only_text,
)
from app.kria.contracts import InferredLabel, RequirementReceipt
from app.schemas.clip_intents import PLACEHOLDER_LABEL_TEXT
from app.services.clip_facts import CAPTURE_ORDER_KEYS

if TYPE_CHECKING:
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest

DURATION_TOLERANCE = 0.10
MAX_REPLY_CHARS = 1200
_CAPTURE_ORDER_KEYS = CAPTURE_ORDER_KEYS  # shared with the render contract and planner
_CAPTURE_BASES = {"capture_time", "route", "capture_order"}


_fold = fold_text

_BRACKETED = re.compile(r"^\s*[\[(<{]\s*(.+?)\s*[\])>}]\s*$")


def _stand_in_core(literal: str | None) -> str | None:
    """The word inside a bracketed stand-in ("[Name]" -> "name"), else None.

    A bracketed literal is a placeholder the creator will replace, not text that is
    printed with its brackets; the server prints the fixed ``PLACEHOLDER_LABEL_TEXT``.
    """
    match = _BRACKETED.match(literal or "")
    return _fold(match.group(1)) if match else None


def _contains_text(haystack: str, wanted: str) -> bool:
    """``wanted`` (already folded) appears in ``haystack`` as whole words.

    Substring matching would call "Go" met by "logo" or "Google Sans".
    """
    if not wanted:
        return False
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", _fold(haystack)) is not None


@dataclass(frozen=True)
class BeatFact:
    """One reaction beat that survives approval's repair (its visual resolved)."""

    trigger: str
    visual_id: str | None = None
    sound: str | None = None


@dataclass(frozen=True)
class SpeechSectionFact:
    """One section of a spoken-excerpt montage as compiled (KRI-282)."""

    kind: str  # "speech" | "montage"
    visual: str = ""  # speech only: "speaker" | "cutaways"
    quote: str = ""
    media_id: str = ""


@dataclass(frozen=True)
class EndpointFact:
    """Where the first or last clip of the plan was filmed (place and landmark texts)."""

    label: str = ""
    places: tuple[str, ...] = ()
    media_id: str = ""


@dataclass(frozen=True)
class PlanFacts:
    """What a drafted plan verifiably contains. Missing facts stay None/empty."""

    clip_ids: tuple[str, ...] = ()
    title: str | None = None
    per_clip_text: dict[str, str] = field(default_factory=dict)
    inferred_text: dict[str, str] = field(default_factory=dict)
    positional_labels: tuple[str, ...] = ()
    # KRI-296: the clips the creator described and gave text for. When set, "text
    # on each shot" is judged against these clips, not every clip in the plan.
    label_scope_clip_ids: tuple[str, ...] = ()
    duration_s: float | None = None
    ordering_basis: str | None = None
    ordering_fallback_clip_ids: tuple[str, ...] = ()
    # KRI-282: the creator's answer to a chronological-vs-grouped conflict, if any.
    ordering_choice: str | None = None
    # KRI-458: how each described group ("start with the football, end at the pub")
    # landed in a unified montage: one status per sequence intent. Empty = none asked.
    sequence_statuses: tuple[str, ...] = ()
    # KRI-503: the groups of that stated sequence that did NOT land where the creator said,
    # as the creator's own words and the spot ("the video that is blue (first)"). Read off
    # the finished order like `sequence_statuses`; empty = every stated group landed.
    sequence_unmet: tuple[str, ...] = ()
    # ... and the stated groups none of whose clips are in the edit (deselected, or no match):
    # absent, not misplaced. A group made only of clips an earlier group already seated, or of
    # Visuals-pool items, is neither (the planner outcome's ``code`` says which).
    sequence_absent: tuple[str, ...] = ()
    # KRI-522: the spots ("first" / "last" / "then") whose stated sequence group landed,
    # read off the same outcomes. None = this plan cannot say (not a unified montage), so a
    # stated first/last clip stays unjudged there instead of failing a plan that seated it.
    sequence_spots_met: tuple[str, ...] | None = None
    texts: tuple[str, ...] = ()
    # Where the title came from: "creator" (their words), "brief" (written from the
    # brief's facts), "default" (nothing to title with), None = unknown.
    title_source: str | None = None
    # Clips whose label was left off because it repeated the previous one (KRI-210).
    repeat_label_clip_ids: tuple[str, ...] = ()
    # The route the creator stated and where the first/last clips were filmed (KRI-208).
    route_start: str | None = None
    route_end: str | None = None
    first_endpoint: EndpointFact | None = None
    last_endpoint: EndpointFact | None = None
    # Seconds the phone appended after the edit (the Kria outro) when the record says so;
    # never in the plan's own length: a 28.0s plan is a ~29.6s file (KRI-210). 0 = unknown.
    outro_s: float = 0.0
    # KRI-190: clips whose label is on a cut shorter than its reading time (the
    # clip itself is too short). A label the viewer cannot read is not "met".
    unreadable_label_clip_ids: tuple[str, ...] = ()
    # KRI-219: what grounded each clip's label ("capture_time", "place", "landmark",
    # ...; absent = unknown), and the zone filming hours were printed in ("" = none).
    per_clip_label_kinds: dict[str, str] = field(default_factory=dict)
    label_timezone: str = ""
    label_timezone_basis: str = ""
    # True when the facts come from an editor payload, which carries literal
    # on-screen text only (no per-clip structure, order or duration).
    editor: bool = False
    # True when the facts describe a plan the server actually rendered (a unified or
    # spoken-excerpt montage record), not a draft that has not been laid out yet. A
    # required order a rendered plan cannot show is a failure; a draft's is pending.
    rendered_output: bool = False
    # True only where the order requirement is bound to an authority that can verify it
    # (a contract-stamped job, or a brief-binding cohort: `build_receipts` sets it from the
    # writer's binding). Legacy / unbound jobs keep the original, softer order verdicts.
    strict_order: bool = False
    # KRI-218: True when the editor facts carry a per-clip text diff (`per_clip_text`,
    # `clip_ids` filled from what the turn actually changed), so per-clip text can be
    # judged for real instead of "can't verify".
    has_clip_structure: bool = False
    # True when an editor payload carries restyled/edited on-screen text elements.
    editor_text_edited: bool = False
    # The strategy's edit format, and how many video clips it renders (None when
    # the footage list was not available to count).
    edit_format: str | None = None
    video_clip_count: int | None = None
    # The item's opt-in Speech cleanup toggle (None = unknown): when on, a render
    # may cut pauses and retakes out of the take.
    speech_cleanup_enabled: bool | None = None
    # Whether approving this draft offers "Clean up speech" (the preflight check is
    # enforced and this project is in its cohort). None = unknown, never assumed.
    speech_cleanup_offered: bool | None = None
    # The strategy's caption style ("none", "clean", "kinetic", "karaoke",
    # "editorial", "auto"); None when the draft carries no strategy.
    caption_style: str | None = None
    # Reaction beats (KRI-178) as approval will keep them: resolved against the
    # creator's owned images by the same repair the compiler runs. None = not
    # checked (no manifest was supplied), so nothing about beats is claimed.
    reaction_beats_available: bool | None = None
    reaction_beats: tuple[BeatFact, ...] | None = None
    # Triggers of beats approval drops because their photo/sticker didn't resolve.
    dropped_beat_triggers: tuple[str, ...] = ()
    closing_requested: bool = False
    closing_visual_id: str | None = None
    closing_badge_requested: bool = False
    closing_badge_id: str | None = None
    # KRI-282: the spoken-excerpt montage as the compiler laid it out, in order.
    # None = this plan is not a spoken-excerpt montage (nothing about speech is claimed).
    speech_sections: tuple[SpeechSectionFact, ...] | None = None
    # Quotes the planner chose that were not found in the clip's speech.
    speech_dropped_quotes: tuple[str, ...] = ()

    @property
    def reaction_beat_count(self) -> int | None:
        return None if self.reaction_beats is None else len(self.reaction_beats)

    @property
    def beat_visual_ids(self) -> tuple[str, ...]:
        return tuple(b.visual_id for b in self.reaction_beats or () if b.visual_id)

    @property
    def beat_sound_triggers(self) -> tuple[str, ...]:
        return tuple(b.trigger for b in self.reaction_beats or () if b.sound)


def _beat_facts(strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest) -> dict[str, Any]:
    """Reaction-beat and closing-shot facts, resolved exactly as approval will.

    The draft strategy carries the model's raw references; approval runs
    ``repair_creator_reaction_beats`` against this same manifest and drops any
    beat whose photo/sticker doesn't resolve (or all of them when the capability
    is off). Running that repair here keeps the receipt honest about what renders.
    """
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.services.creator_capabilities import (  # noqa: PLC0415
        CAPABILITY_REACTION_BEATS,
        repair_creator_reaction_beats,
    )

    capability = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
    available = bool(capability is not None and capability.available)
    try:
        parsed = CreativeStrategy.model_validate(dict(strategy))
    except ValueError:
        return {"reaction_beats_available": available}
    repaired, _notices = repair_creator_reaction_beats(manifest, parsed)
    kept = list(repaired.reaction_beats or [])
    kept_ids = {beat.beat_id for beat in kept}
    raw_closing = parsed.closing_media
    closing = repaired.closing_media
    return {
        "reaction_beats_available": available,
        "reaction_beats": tuple(
            BeatFact(trigger=b.trigger, visual_id=b.visual_id, sound=b.sound) for b in kept
        ),
        "dropped_beat_triggers": tuple(
            dict.fromkeys(
                b.trigger for b in parsed.reaction_beats or [] if b.beat_id not in kept_ids
            )
        ),
        "closing_requested": raw_closing is not None,
        "closing_visual_id": closing.visual_id if closing is not None else None,
        "closing_badge_requested": bool(raw_closing and raw_closing.badge_visual_id),
        "closing_badge_id": closing.badge_visual_id if closing is not None else None,
    }


def _video_clip_count(strategy: Mapping[str, Any], manifest: ResolvedCreatorManifest) -> int:
    videos = [m.media_id for m in manifest.media if m.kind == "video"]
    selected = {str(x) for x in strategy.get("selected_media_ids") or []}
    if selected and strategy.get("media_scope") != "all":
        videos = [media_id for media_id in videos if media_id in selected]
    return len(videos)


def plan_facts_from_strategy(
    strategy: Mapping[str, Any] | None,
    *,
    clip_ids: Iterable[str] = (),
    manifest: ResolvedCreatorManifest | None = None,
    speech_cleanup_enabled: bool | None = None,
    speech_cleanup_offered: bool | None = None,
) -> PlanFacts:
    """Read verifiable facts off a serialized ``CreativeStrategy``.

    ``manifest`` is the turn's resolved creator manifest. Without it the beat,
    closing-shot and clip-count facts stay unknown (never guessed).
    ``speech_cleanup_offered`` is whether approval will offer "Clean up speech"
    (the caller's cohort check); None keeps the cleanup receipt from promising it.
    """
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
            if assignment.get("grounding") not in {"creator_text", "placeholder"}:
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
    extra: dict[str, Any] = {}
    if manifest is not None:
        extra = _beat_facts(strategy, manifest)
        extra["video_clip_count"] = _video_clip_count(strategy, manifest)
    edit_format = strategy.get("edit_format")
    caption_style = strategy.get("caption_style")
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
        edit_format=str(edit_format) if edit_format else None,
        speech_cleanup_enabled=speech_cleanup_enabled,
        speech_cleanup_offered=speech_cleanup_offered,
        caption_style=str(caption_style) if caption_style else None,
        **extra,
    )


def _endpoint_fact(raw: object) -> EndpointFact | None:
    if not isinstance(raw, Mapping):
        return None
    places = tuple(
        str(row["text"])
        for row in raw.get("places") or []
        if isinstance(row, Mapping) and row.get("text")
    )
    label = str(raw.get("label") or "")
    media_id = str(raw.get("media_id") or "")
    return (
        EndpointFact(label=label, places=places, media_id=media_id) if (label or places) else None
    )


def _declared_outro_s(record: Mapping[str, Any]) -> float:
    """The outro the record says the finished video carries, else 0 (unknown).

    The phone declares its brand tail only when it exports, after the plan and its receipts
    exist, so a plan-time record has none and the outro is never mentioned on an assumption.
    """
    from app.kria.device_render import BRAND_TAIL_SECONDS  # noqa: PLC0415

    declared = record.get("brand_tail")
    if isinstance(declared, str) and declared in BRAND_TAIL_SECONDS:
        return BRAND_TAIL_SECONDS[declared]
    stored = record.get("brand_tail_s")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
        return float(stored)
    return 0.0


def _sequence_label(name: str) -> str:
    """``first: the video that is blue`` (the planner's outcome name) -> ``the video that is
    blue (first)``, the way a creator would say it back."""
    spot, _, words = name.partition(": ")
    if words and spot in ("first", "last", "then"):
        return f"{words} ({spot})"
    return name or "the order you described"


def _sequence_problems(record: Mapping[str, Any], codes: tuple[str | None, ...]) -> tuple[str, ...]:
    """The stated-sequence groups of a unified-montage record that did not land, by outcome
    ``code`` (KRI-503). A ``then`` group wholly inside an earlier group, and a Visuals-only
    group, are not "a described group missed its place"; a row without a code (an older or
    hand-built record) counts as misplaced, exactly as before."""
    out: list[str] = []
    for row in record.get("intent_outcomes") or []:
        if not isinstance(row, Mapping) or row.get("op") != "order" or row.get("status") == "met":
            continue
        code = row.get("code")
        if code == "visual" or (code == "contained" and row.get("position") == "then"):
            continue
        if code in codes:
            out.append(_sequence_label(str(row.get("name") or "")))
    return tuple(out)


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
    route = record.get("route") if isinstance(record.get("route"), Mapping) else {}
    endpoints = (
        record.get("endpoint_places") if isinstance(record.get("endpoint_places"), Mapping) else {}
    )
    reasons = record.get("dropped_label_reasons")
    repeats = (
        tuple(str(k) for k, v in reasons.items() if v == "repeat")
        if isinstance(reasons, Mapping)
        else ()
    )
    return PlanFacts(
        clip_ids=tuple(str(c) for c in record.get("clip_ids") or []),
        title=str(title) if title else None,
        title_source=str(record["title_source"]) if record.get("title_source") else None,
        repeat_label_clip_ids=repeats,
        route_start=str(route["start"]) if route.get("start") else None,
        route_end=str(route["end"]) if route.get("end") else None,
        first_endpoint=_endpoint_fact(endpoints.get("first")),
        last_endpoint=_endpoint_fact(endpoints.get("last")),
        outro_s=_declared_outro_s(record),
        per_clip_text=per_clip,
        inferred_text=inferred,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        rendered_output=True,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in record.get("ordering_fallback_clip_ids") or []
        ),
        ordering_choice=str(record["ordering_choice"]) if record.get("ordering_choice") else None,
        sequence_statuses=tuple(
            str(row.get("status"))
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping) and row.get("op") == "order"
        ),
        sequence_spots_met=tuple(
            str(row.get("position") or "then")
            for row in record.get("intent_outcomes") or []
            if isinstance(row, Mapping) and row.get("op") == "order" and row.get("status") == "met"
        ),
        sequence_unmet=_sequence_problems(record, ("misplaced", None, "contained")),
        sequence_absent=_sequence_problems(record, ("absent", "unresolved")),
        texts=tuple(
            str(text) for text in (title, record.get("closing_title"), *per_clip.values()) if text
        ),
        label_scope_clip_ids=tuple(str(c) for c in record.get("label_scope_clip_ids") or []),
        unreadable_label_clip_ids=tuple(str(c) for c in record.get("short_label_clip_ids") or []),
        per_clip_label_kinds={
            str(row["media_id"]): str(row["fact_kind"])
            for row in labels
            if row.get("text") and row.get("fact_kind")
        },
        label_timezone=str(record.get("label_timezone") or ""),
        label_timezone_basis=str(record.get("label_timezone_basis") or ""),
    )


def plan_facts_from_speech_montage(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a spoken-excerpt montage record (KRI-282).

    ``record`` is what ``phone_speech_montage_job`` stored: the sections the
    compiler actually put on the timeline (each excerpt already grounded to
    word timings) and the quotes that could not be grounded.
    """
    record = record or {}
    sections = tuple(
        SpeechSectionFact(
            kind=str(row.get("kind") or ""),
            visual=str(row.get("visual") or ""),
            quote=str(row.get("quote") or ""),
            media_id=str(row.get("media_id") or ""),
        )
        for row in record.get("sections") or []
        if isinstance(row, Mapping)
    )
    planned = record.get("planned") if isinstance(record.get("planned"), Mapping) else {}
    dropped = tuple(
        str(row.get("quote"))
        for row in (planned or {}).get("dropped") or []
        if isinstance(row, Mapping) and row.get("quote")
    )
    duration = record.get("duration_s")
    basis = record.get("ordering_basis")
    return PlanFacts(
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        speech_sections=sections,
        speech_dropped_quotes=dropped,
        ordering_basis=str(basis) if basis else None,
        rendered_output=True,
    )


def _editor_payload_duration(payload: Mapping[str, Any]) -> float | None:
    """Output length implied by the payload's timeline slots, or None when unprovable.

    Only exact-second slots count (beat-sized slots need the audio grid) and a
    speed change divides the window. Crossfade overlap is ignored, which the
    tolerance absorbs.
    """
    slots = payload.get("timeline_slots") if isinstance(payload, Mapping) else None
    if not isinstance(slots, list) or not slots:
        return None
    total = 0.0
    for slot in slots:
        if not isinstance(slot, Mapping):
            return None
        if slot.get("removed"):
            continue
        duration = slot.get("duration_s")
        if not isinstance(duration, (int, float)) or duration <= 0:
            return None
        rate = slot.get("playback_rate")
        total += float(duration) / (
            float(rate) if isinstance(rate, (int, float)) and rate > 0 else 1.0
        )
    return total or None


def plan_facts_from_editor_payload(
    payload: Mapping[str, Any] | None,
    text_diff: Iterable[Mapping[str, Any]] | None = None,
    changes: Iterable[str] | None = None,
) -> PlanFacts:
    """Editor drafts expose literal on-screen text, plus (KRI-218) the turn's text diff.

    ``text_diff`` is the compiler's ``[{id, clip_id, role, before, after}]``. Clip-linked
    entries fill ``per_clip_text`` / ``clip_ids`` (only the clips this turn touched), so a
    per-clip requirement is checked against what was really written.
    """
    if not payload:
        return PlanFacts()
    # Do not walk every payload value: source requests, paths, operation names,
    # and arbitrary metadata may repeat the creator's literal without placing it
    # on screen. Only explicit output text lanes count as visible evidence.
    strings: list[str] = []
    per_clip: dict[str, str] = {}
    title: str | None = None

    def take_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    raw_title = payload.get("title") if isinstance(payload, Mapping) else None
    if isinstance(raw_title, Mapping):
        title = take_text(raw_title.get("text"))
    else:
        title = take_text(raw_title)
    if title:
        strings.append(title)

    for lane_name in ("text_elements", "bars"):
        rows = payload.get(lane_name) if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            text = take_text(row.get("text"))
            if text is None:
                continue
            strings.append(text)
            clip = row.get("clip_id")
            if isinstance(clip, str) and clip:
                per_clip[clip] = text
    for entry in text_diff or ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("after"), str):
            continue
        clip = entry.get("clip_id")
        if isinstance(clip, str) and clip:
            per_clip[clip] = entry["after"]
            strings.append(entry["after"])
        elif entry.get("role") == "title":
            title = entry["after"]
            strings.append(title)
    # The compiler's own change list says the timeline was re-sorted by filming time.
    ordered_by_capture = any(
        str(change).startswith("Order clips by filming time") for change in changes or ()
    )
    return PlanFacts(
        ordering_basis="capture_time" if ordered_by_capture else None,
        duration_s=_editor_payload_duration(payload),
        editor_text_edited=bool(
            isinstance(payload, Mapping)
            and isinstance(payload.get("text_elements"), list)
            and payload.get("text_elements")
        ),
        texts=tuple(strings),
        editor=True,
        has_clip_structure=bool(per_clip),
        per_clip_text=per_clip,
        clip_ids=tuple(per_clip),
        title=title,
    )


def _receipt(
    req: BriefRequirement,
    status: str,
    reason: str | None,
    inferred: Iterable[str] = (),
    labels: Iterable[InferredLabel] = (),
    *,
    verification: str | None = None,
    stage: str | None = None,
) -> RequirementReceipt:
    target_media_ids = [req.scope.split(":", 1)[1]] if req.scope.startswith("clip:") else []
    return RequirementReceipt(
        requirement_id=req.id,
        status=status,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        target_media_ids=target_media_ids,
        reason=reason[:300] if reason else None,
        inferred=list(dict.fromkeys(str(x) for x in inferred))[:24],
        inferred_labels=list(labels)[:24],
    )


def _guess_labels(facts: PlanFacts, only: str | None = None) -> list[InferredLabel]:
    """The guessed names with the clip each belongs to (plan order first)."""
    ids = list(facts.clip_ids)
    order = [c for c in ids if c in facts.inferred_text] or list(facts.inferred_text)
    if only is not None:
        order = [c for c in order if c == only]
    return [
        InferredLabel(
            text=facts.inferred_text[clip][:120],
            media_id=clip,
            clip_index=ids.index(clip) if clip in ids else None,
        )
        for clip in order
    ]


_CANT_CHECK_EDITOR_CLIP_TEXT = "I can't verify per-clip text on an editor edit."
_CANT_CHECK_TARGET_CLIP_TEXT = "I can't verify text for that target clip in this draft."


def _check_per_clip_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if (
        req.scope == "per_clip"
        and wants_hour_only_text(req.description, req.literal)
        and facts.per_clip_text
    ):
        # "Just the hour, no minutes" is a FORMAT: met only when every label the plan
        # actually carries is a bare hour, never because the labels merely exist.
        bad = [c for c, t in facts.per_clip_text.items() if not re.fullmatch(r"\d{1,2}", t.strip())]
        if bad:
            return _receipt(
                req,
                "partial",
                f"{len(bad)} of {len(facts.per_clip_text)} labels still show more than the "
                "hour. A re-render can't reformat them; ask me again to change them in the editor.",
            )
    wanted = _fold(req.literal or "")
    core = _stand_in_core(req.literal)
    if core is not None:
        # KRI-282: "[Name]" is judged by the stand-in the plan actually printed.
        stand_in = _fold(PLACEHOLDER_LABEL_TEXT)
        printed = any(_fold(t) == stand_in for t in facts.per_clip_text.values())
        wanted = stand_in if printed else core
    if facts.editor and not facts.has_clip_structure:
        # An editor payload's loose text list cannot prove which clip owns a
        # caption. In particular it must never fulfill `clip:<id>` because an
        # unrelated label contains the same literal.
        if req.scope.startswith("clip:") or (
            wanted and any(_contains_text(t, wanted) for t in facts.texts)
        ):
            return _receipt(req, "partial", _CANT_CHECK_EDITOR_CLIP_TEXT)
        if wanted:
            return _receipt(req, "partial", "That exact text isn't in this edit.")
        return _receipt(req, "partial", _CANT_CHECK_EDITOR_CLIP_TEXT)
    if facts.editor and wanted and _wants_exact_text(req):
        # "just say X" / "X only": every clip this turn touched must read exactly X.
        off = [c for c, t in facts.per_clip_text.items() if _fold(t) != wanted]
        if off:
            total = len(facts.per_clip_text)
            return _receipt(
                req,
                "partial",
                f"{len(off)} of {total} text{'s' if total != 1 else ''} "
                f"didn't end up reading exactly \u201c{req.literal}\u201d.",
            )
    if (
        not facts.editor
        and wants_filming_time_text(req.kind, req.scope, req.literal, req.description, req.facts)
        and facts.per_clip_text
    ):
        judged = _check_filming_time_text(req, facts)
        if judged is not None:
            return judged
    ids = facts.clip_ids

    def text_for(index: int, clip: str) -> str | None:
        if clip in facts.per_clip_text:
            return facts.per_clip_text[clip]
        if index < len(facts.positional_labels):
            return facts.positional_labels[index]
        return None

    if req.scope.startswith("clip:"):
        clip = req.scope.split(":", 1)[1]
        if clip not in ids:
            return _receipt(req, "partial", _CANT_CHECK_TARGET_CLIP_TEXT)
        index = ids.index(clip) if clip in ids else len(ids)
        value = text_for(index, clip)
        if value is None and clip in facts.repeat_label_clip_ids:
            return _receipt(
                req,
                "partial",
                "That clip's label repeated the clip before it, so I left it off.",
            )
        if value is None:
            return _receipt(req, "not_possible", "That clip didn't get its own text in this draft.")
        if wanted and not _contains_text(value, wanted):
            return _receipt(req, "partial", "That clip's text isn't the exact text you gave.")
        guessed = _guess_labels(facts, only=clip)
        return _receipt(req, "met", None, [g.text for g in guessed], guessed)

    scope = set(facts.label_scope_clip_ids)
    judged_ids = [(i, clip) for i, clip in enumerate(ids) if not scope or clip in scope]
    total = len(judged_ids)
    if ids:
        count = sum(1 for i, clip in judged_ids if text_for(i, clip) is not None)
    else:
        count = max(len(facts.per_clip_text), len(facts.positional_labels))
    guessed = _guess_labels(facts)
    inferred = [g.text for g in guessed]
    if wanted and count:
        given = [*facts.per_clip_text.values(), *facts.positional_labels]
        if not any(_contains_text(t, wanted) for t in given):
            return _receipt(
                req, "partial", "The clips don't carry the exact text you gave.", inferred, guessed
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
                guessed,
            )
        return _receipt(req, "met", None, inferred, guessed)
    if count == 0:
        return _receipt(
            req,
            "not_possible",
            "No clip got its own text in this draft."
            if not total
            else f"None of the {total} clips got its own text in this draft.",
        )
    reason = f"Text landed on {count} of {total} clips." if total else f"Text on {count} clips."
    repeats = len(facts.repeat_label_clip_ids)
    if repeats:
        # Left off on purpose: the same name twice in a row adds nothing to the viewer.
        reason = (
            f"{reason[:-1]}; {repeats} more repeated the label before, so I left "
            f"{'them' if repeats != 1 else 'it'} off."
        )
    return _receipt(req, "partial", reason, inferred, guessed)


def _check_filming_time_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """ "Add the hour to each video": place names or nothing are NOT the hour (KRI-219)."""
    kinds = facts.per_clip_label_kinds
    if not kinds:
        return None  # grounding unknown: fall through to the coverage check
    total = len(facts.clip_ids) or len(facts.per_clip_text)
    timed = [c for c in facts.per_clip_text if kinds.get(c) == "capture_time"]
    if not timed:
        return _receipt(
            req,
            "not_possible",
            "The labels are place names, not the hour each clip was filmed.",
        )
    if len(timed) < total:
        missing = total - len(timed)
        return _receipt(
            req,
            "partial",
            f"Filming hour on {len(timed)} of {total} clips; "
            f"{missing} {'have' if missing != 1 else 'has'} no filming time.",
        )
    if facts.label_timezone:
        # Delivered, but the creator must know which zone the hours are in.
        from app.services.clip_facts import timezone_note  # noqa: PLC0415

        return _receipt(req, "met", timezone_note(facts.label_timezone, facts.label_timezone_basis))
    return None


_NAME_IN_REASON_CHARS = 32


def _short(name: str) -> str:
    text = " ".join(name.split())
    return text if len(text) <= _NAME_IN_REASON_CHARS else text[: _NAME_IN_REASON_CHARS - 1] + "…"


def _names_place(endpoint: EndpointFact | None, name: str) -> bool:
    """True when the creator's ``name`` is one of the places recorded for ``endpoint``.

    Matches whole words either way round, ignoring case and Turkish diacritics, against
    the full geocode and each comma-separated part of it ("Fatih" in "Eminönü, Fatih,
    İstanbul"). A one-word name never matches inside a longer word.
    """
    if endpoint is None:
        return False
    wanted = loose_text(name)
    if not wanted:
        return False
    for place in (*endpoint.places, endpoint.label):
        for part in (place, *(p for p in place.split(",") if p.strip())):
            seen = loose_text(part)
            if not seen:
                continue
            if seen == wanted:
                return True
            for hay, needle in ((seen, wanted), (wanted, seen)):
                if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
                    return True
    return False


def _route_reversed_reason(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """A plain-language reason when the clips were filmed in the opposite direction.

    The plan follows filming order (capture time). If the creator stated a route and the
    first/last clips' places say it was walked the other way, the receipt says what was
    seen and what was done and asks which to follow. It never silently reorders, and
    stays quiet unless a side positively matches the reverse and nothing matches forward.
    """
    if facts.ordering_basis not in _CAPTURE_BASES:
        return None
    start = first_text(req.facts, START_KEYS) or facts.route_start
    end = first_text(req.facts, END_KEYS) or facts.route_end
    if not start or not end or facts.first_endpoint is None or facts.last_endpoint is None:
        return None
    first, last = facts.first_endpoint, facts.last_endpoint
    untimed = set(facts.ordering_fallback_clip_ids)
    if (first.media_id and first.media_id in untimed) or (
        last.media_id and last.media_id in untimed
    ):
        # An endpoint without a capture time sits in an attachment slot, not where it was
        # filmed: it says nothing about the direction the route was walked.
        return None
    forward = _names_place(first, start) or _names_place(last, end)
    backward = _names_place(first, end) or _names_place(last, start)
    if forward or not backward:
        return None
    saw_first, saw_last = _short(first.label or end), _short(last.label or start)
    return (
        f"Your clips were filmed starting at {saw_first} and ending at {saw_last}, the reverse "
        f"of the route you gave ({_short(start)} → {_short(end)}). I kept filming order; "
        "tell me if you want your route order instead."
    )


# A strategy draft never records its clip order (only a unified montage plan
# does), so a draft-time order check can only say it couldn't tell.
_CANT_CONFIRM_ORDER = "I can't confirm the order this draft uses."
_CANT_CHECK_ORDER_RULE = "I can't verify this ordering automatically."
_ARRIVAL_BASES = frozenset({"attachment", "creator_order"})
_ORDER_NOT_APPLIED = (
    "I couldn't match your description to the clips, so they stay in the order you attached them."
)
# KRI-470 PR-G: what a required order says when it was not met.
_ORDER_NOT_RECORDED = "I couldn't confirm the order your edit used."
_ORDER_RULE_NOT_APPLIED = (
    "I can't verify this ordering rule, and the clips are not in an order I can show matches it."
)
_ORDER_VERIFIED_ELSEWHERE = frozenset({"song_time", "confirmed", "editor", "attachment_order"})
_PREFERENCE_STRENGTHS = frozenset({"preference", "prefer", "optional", "soft", "nice_to_have"})


def _order_is_required(req: BriefRequirement) -> bool:
    """An order the creator asked for is REQUIRED, exactly as the render contract pins it
    (`order_required`). Only an order explicitly marked as a preference stays optional."""
    strength = str(req.facts.get("strength") or req.facts.get("priority") or "").casefold()
    return req.facts.get("required") is not False and strength not in _PREFERENCE_STRENGTHS


_ORDER_ANCHOR_FACTS = (("first_clip", "first", "start with"), ("last_clip", "last", "end with"))


def _unplaced_order_anchor(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """Why a stated first/last clip is unconfirmed, in the creator's own words, else None."""
    if facts.sequence_spots_met is None or not facts.rendered_output:
        return None
    said: list[str] = []
    for key, spot, verb in _ORDER_ANCHOR_FACTS:
        words = req.facts.get(key)
        if isinstance(words, str) and words.strip() and spot not in facts.sequence_spots_met:
            said.append(f"{verb} {words.strip()}")
    if not said:
        return None
    return f"You asked me to {' and '.join(said)}, but I can't confirm that clip is placed there."


def _check_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A required order is met, partly met (an honest fallback), or FAILED. Neutral
    (unchecked) is only for an optional preference, and for a draft that has not been
    laid out yet (its order is judged when it renders)."""
    key = str(req.facts.get("key") or req.facts.get("by") or "").casefold()
    required = facts.strict_order and _order_is_required(req)
    unmet = "not_possible" if required else "partial"
    if not facts.ordering_basis:
        if required and facts.rendered_output:
            return _receipt(req, "not_possible", _ORDER_NOT_RECORDED)
        return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
    basis = facts.ordering_basis
    reversed_reason = _route_reversed_reason(req, facts)
    if reversed_reason is not None:
        return _receipt(req, "partial", reversed_reason)
    if key in _CAPTURE_ORDER_KEYS:
        if basis not in _CAPTURE_BASES:
            return _receipt(
                req,
                unmet,
                f"This draft is ordered by {basis.replace('_', ' ')}, not the order you asked for.",
            )
    elif facts.sequence_statuses:
        # The creator described the order in their own words; the plan placed (or
        # failed to place) each group and recorded which (KRI-458).
        landed = sum(status == "met" for status in facts.sequence_statuses)
        if landed < len(facts.sequence_statuses):
            return _receipt(
                req,
                "partial" if landed or not required else "not_possible",
                "some of the groups you named are not where you said",
            )
    elif basis in _ARRIVAL_BASES and key != basis:
        # An order only the creator's words describe, and nothing in the plan applied
        # it: say so, never let a silent attachment order read as "done" (KRI-458).
        return _receipt(req, unmet, _ORDER_NOT_APPLIED)
    elif not key or key != basis:
        # Nothing here can confirm an ordering this checker has no rule for. When the
        # order is required, "can't confirm" is not a pass: the plan is in some other
        # order than the one asked for. Except where another authority owns the order and
        # verifies it: a lip-sync montage's song placement (the contract skips a creator's
        # "use this order" answer there, #1451, and the lip-sync receipts verify the
        # takes) and the contract's own confirmed / editor / answered order ids. A rule
        # the checker has no key for stays unjudged there instead of blocking a render
        # that placed it.
        if required and basis not in _ORDER_VERIFIED_ELSEWHERE:
            return _receipt(req, "not_possible", _ORDER_RULE_NOT_APPLIED)
        return _receipt(req, "partial", _CANT_CHECK_ORDER_RULE)
    if (
        key in _CAPTURE_ORDER_KEYS
        and facts.strict_order
        and (facts.sequence_unmet or facts.sequence_absent)
    ):
        # KRI-503: "chronological order, starting with the blue video". The basis matching is
        # not enough: the stated clip must really be where the creator put it. The render
        # contract pins that same seating, so a plan that misses it would be refused after
        # the render; say so before.
        said = []
        if facts.sequence_unmet:
            said.append(f"these aren't where you asked: {', '.join(facts.sequence_unmet)}")
        if facts.sequence_absent:
            said.append(f"I found no clips for: {', '.join(facts.sequence_absent)}")
        return _receipt(req, unmet, f"Your clips are in filming order, but {'; '.join(said)}.")
    missing_anchor = _unplaced_order_anchor(req, facts)
    if missing_anchor is not None:
        # KRI-522: "chronological, starting with the blue video" lives in the brief as
        # `first_clip`. A plan that recorded no placed first/last group cannot claim it.
        # Partial, not a block: the edit is still the creator's filming order, and saying
        # so beats failing a render that is otherwise right.
        return _receipt(req, "partial", missing_anchor)
    if facts.ordering_fallback_clip_ids:
        n = len(facts.ordering_fallback_clip_ids)
        return _receipt(
            req,
            "partial",
            f"{n} clip{'s' if n != 1 else ''} had no capture time, so I kept "
            f"{'their' if n != 1 else 'its'} attachment order.",
        )
    if key in _CAPTURE_ORDER_KEYS and facts.ordering_choice == "group_first":
        # Honest: the creator picked grouping over strict filming order, so the order
        # holds inside each group only.
        return _receipt(
            req, "met", "you chose grouping first, so it's in filming order inside each group"
        )
    return _receipt(req, "met", None)


# ------------------------------------------------ requirement shape recognisers
#
# The brief extractor has no dedicated kind for "keep my whole take" or "pop up X
# when I say Y": the first arrives as a `timing` requirement with no number, the
# second as `style`/`audio`/`select`. These deterministic recognisers read the
# creator's framing (description/literal, Turkish-aware fold) and `facts` keys.

_WHOLE_TAKE_RE = re.compile(
    r"\b(whole|full|entire|complete|original) (take|clip|video|recording|footage)\b"
    r"|\b(exactly )?as (i )?(recorded|filmed|shot) it\b|\bexactly as (i )?(recorded|filmed)\b"
    r"|\bas recorded\b|\bstart to finish\b|\bbeginning to (the )?end\b"
    r"|\b(don'?t|do not|never|no) (cut|trim|shorten)|\buncut\b|\bfull[- ]length\b"
    r"|\bbaştan sona\b|\bolduğu gibi\b|\bkesmeden\b|\bhiç kesme|\btamam[iı]n[iı]\b"
)
_BEAT_CUE_RE = re.compile(
    r"\bwhen (i|you|we) (say|mention|hear|talk about|name)\b|\bwhen you hear\b"
    r"|\bpop(s|ping)?[- ]?(up|in|ups|ins)\b|\bat (the |each |every )?(exact |spoken |specific )?"
    r"words?\b|\b(sticker|stamp)s?\b|\bword[- ]triggered\b"
    r"|dediğimde|deyince|söylediğimde|bahsettiğimde|\bçikartma"
)
_CLOSING_RE = re.compile(
    r"\b(finish|end|close|wrap up|wrap) (on|with)\b|\bending (on|with|shot)\b"
    r"|\bclosing (shot|photo|image|picture|frame)\b|\blast (shot|frame)\b"
    r"|\bbitir|\bkapan[iı]ş"
)
_SOUND_RE = re.compile(
    r"\b(sound|sounds|sfx|buzzer|ding|beep|whoosh|swoosh|boing|horn|applause)\b|\bses\b|\befekt"
)
_VISUAL_RE = re.compile(
    r"\b(sticker|stamp|badge|photo|picture|image|pic|flag|logo|emoji)s?\b"
    r"|fotoğraf|resim|görsel|çikartma|bayrak"
)
# "Cut out the long pauses / the retake / the ums": a speech-cleanup ask is a removal
# verb plus something speech cleanup removes (or a named retake). The brief extractor
# files it as `timing` (often with the length the creator also named), `audio`,
# `style` or `select`; the ask itself is read from the creator's words. Patterns are
# written against `_fold` output: lower case, every Turkish ı folded to i.
_CLEANUP_NOUN_RE = re.compile(
    r"\b(pauses?|silences?|dead air|dead space|retakes?|fillers?|filler words?|stumbles?"
    r"|false starts?)\b|\b(um+s?|uh+s?|er+s?|hmm+s?)\b"
    r"|\bduraklama|\bsessizlik|\bdolgu|\btak[iı]lma"
)
_CLEANUP_VERB_RE = re.compile(
    r"\b(cut|remove|trim|take out|get rid of|drop|delete|skip|lose|edit out|without|no)\b"
    r"|\bkes|\bçikar|\bsil\b|\bolmasin|\bolmadan"
)
# A specific stretch of speech named for removal ("the part where I say ...", a retake,
# a quoted line): speech cleanup cuts pauses and non-word sounds, never spoken words.
_NAMED_CUT_RE = re.compile(
    r"\bretakes?\b|\bfalse starts?\b|\b(the )?(part|bit|place|section|moment) where\b"
    r"|\bwhere i (say|said|start|stumble)|\blet me (start|try|say|do) (that|it|this|again|over|one)"
    r"|\bstart (that|it|this|one) (again|over)\b|\bstart (again|over)\b"
    r"|\btry (that|it|this) again\b"
    r"|\bscratch that\b|\bwhere was i\b"
    r"|\btekrar(dan)? (baştan|söyle)|\bnerede kalmistim|\bdediğim (yer|kisim)|\bbaştan (al|başla)"
)
# The opposite ask: the pauses are wanted. Never read as cleanup.
_KEEP_PAUSES_RE = re.compile(
    r"\b(don'?t|do not|never|without) (remove|cut|cutting|trim|touch|take out|delete|skip"
    r"|tighten|clean)\b.{0,40}\b(pauses?|silences?|ums?|breaths?|gaps?)\b"
    r"|\b(keep|leave|preserve|maintain) (my |the |all |all the |some |natural |those |a )*"
    r"(pauses?|silences?|ums?|breaths?|rhythm|dramatic pause)\b"
    r"|\bduraklamalar[iı]? kalsin|\bdokunma"
)
_CAPTION_RE = re.compile(r"\b(captions?|subtitles?|karaoke)\b|\baltyazi")
_WORD_CAPTION_RE = re.compile(
    r"\bkaraoke\b|\bword[- ]by[- ]word\b|\b(one )?word at a time\b"
    r"|\bhighlight(ed|ing|s)? (the |each |every )?(key |spoken |current |said )?words?\b"
    r"|\bwords? (highlighted|light(s|ing)? up|lit up)\b|\bkelime kelime\b|\bvurgula"
)
_NO_CAPTION_RE = re.compile(
    r"\b(no|without|remove|drop|turn off|hide|skip) (the )?(captions?|subtitles?)\b"
    r"|\b(captions?|subtitles?) off\b|\baltyazisiz\b|\baltyazi (olmasin|istemiyorum|yok)"
)
# A captions ask about look, place or language is not judged by the style alone.
_CAPTION_DETAIL_RE = re.compile(
    r"\b(colou?rs?|yellow|white|red|blue|green|black|pink|orange|purple|lime|font|bold|italic"
    r"|sizes?|bigger|big|small|smaller|larger|huge|tiny|top|bottom|middle|cent(er|re)|left|right"
    r"|higher|lower|outline|shadow|stroke|uppercase|lowercase|caps|language|translat\w*"
    r"|turkish|english|german|spanish|french|italian|arabic|dutch|portuguese)\b"
    r"|\brenk|\bsari\b|\bbeyaz\b|\bbüyük|\bküçük|\büst|\balt(ta|a)\b|\btürkçe|\bingilizce"
    r"|\byazi tipi|\bkalin"
)
_TRIGGER_FACT_KEYS = (
    "triggers",
    "trigger",
    "trigger_words",
    "words",
    "cue_words",
    "cues",
    "at_words",
    "when_i_say",
    "spoken_words",
    "keywords",
)
_BEAT_KINDS = frozenset({"style", "audio", "select"})
_CLEANUP_KINDS = frozenset({"timing", "style", "audio", "select"})
# Edits spined by the creator's own speech: captions come from it, and speech
# cleanup (pauses, filler sounds) is how it gets tightened.
_SPEECH_FORMATS = frozenset({"subtitled", "talking_head", *NARRATED_EDIT_FORMATS})
_QUOTES = '"\u201c\u201d\u00ab\u00bb'
_QUOTED_RE = re.compile(rf"[{_QUOTES}]([^{_QUOTES}]{{1,80}})[{_QUOTES}]")
_MAX_NAMED_IN_REASON = 6


def _req_text(req: BriefRequirement) -> str:
    return _fold(" ".join(x for x in (req.description, req.literal) if x))


def _wants_whole_take(req: BriefRequirement) -> bool:
    if req.kind != "timing" or _has_duration_target(req):
        return False
    return bool(req.facts.get("keep_whole_take") or _WHOLE_TAKE_RE.search(_req_text(req)))


def _fact_triggers(req: BriefRequirement) -> list[str]:
    found: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str):
            found.extend(part for part in re.split(r"\s*[,;/]\s*", value) if part.strip())
        elif isinstance(value, Mapping):
            words = [value[k] for k in ("trigger", "word", "phrase") if k in value]
            if words:
                take(words)  # [{"trigger": "pasta", "visual": ...}, ...]
            else:
                # {"pasta": "photo", ...}: the keys are the spoken words.
                found.extend(str(key) for key in value if isinstance(key, str))
        elif isinstance(value, list):
            for item in value:
                take(item)

    for key in _TRIGGER_FACT_KEYS:
        if key in req.facts:
            take(req.facts[key])
    return found


def _named_triggers(req: BriefRequirement) -> list[str]:
    """The spoken words the creator named: from `facts`, else quoted in the text."""
    named = _fact_triggers(req)
    if not named:
        for field_value in (req.description, req.literal):
            named.extend(_QUOTED_RE.findall(field_value or ""))
    out: dict[str, str] = {}
    for name in named:
        cleaned = " ".join(str(name).split())
        if cleaned and len(cleaned) <= 80:
            out.setdefault(_fold(cleaned), cleaned)
    return list(out.values())


def _wants_beats(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_KINDS:
        return False
    return bool(_fact_triggers(req) or _BEAT_CUE_RE.search(_req_text(req)))


def _wants_closing(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_KINDS:
        return False
    closing_fact = req.facts.get("closing") or req.facts.get("closing_media")
    return bool(closing_fact or _CLOSING_RE.search(_req_text(req)))


def _wants_cleanup(req: BriefRequirement) -> bool:
    """A speech-cleanup ask (pauses, retakes, filler) in the creator's own words."""
    if req.kind not in _CLEANUP_KINDS or _wants_whole_take(req):
        return False
    if _wants_beats(req) or _wants_closing(req) or _wants_speech(req):
        return False
    text = _req_text(req)
    if _KEEP_PAUSES_RE.search(text) or not _CLEANUP_VERB_RE.search(text):
        return False
    return bool(_CLEANUP_NOUN_RE.search(text) or _NAMED_CUT_RE.search(text))


def _wants_named_cuts(req: BriefRequirement) -> bool:
    """The ask names a stretch of speech to remove, not just pauses and sounds."""
    text = _req_text(req)
    if _NAMED_CUT_RE.search(text):
        return True
    # A quoted line is a spoken stretch; a quoted "um" is a filler sound.
    return any(
        not _CLEANUP_NOUN_RE.search(_fold(quoted))
        for quoted in _QUOTED_RE.findall(req.description or "")
    )


def _wants_captions(req: BriefRequirement) -> bool:
    """A captions ask ("add captions", "karaoke captions", "no captions")."""
    if req.kind in _BEAT_KINDS:
        if _wants_beats(req) or _wants_closing(req) or _wants_speech(req):
            return False
    elif not (req.kind == "text" and req.scope == "global" and not req.literal):
        return False
    return bool(_CAPTION_RE.search(_req_text(req)))


def _wants_no_captions(req: BriefRequirement) -> bool:
    return bool(_NO_CAPTION_RE.search(_req_text(req)))


def _wants_word_captions(req: BriefRequirement) -> bool:
    return bool(_WORD_CAPTION_RE.search(_req_text(req)))


def _has_duration_target(req: BriefRequirement) -> bool:
    target = req.facts.get("duration_s")
    return isinstance(target, (int, float)) and not isinstance(target, bool) and target > 0


def _trigger_heard(named: str, spoken: str) -> bool:
    a, b = _fold(named), _fold(spoken)
    return a == b or _contains_text(spoken, a) or _contains_text(named, b)


def _names(values: Iterable[str]) -> str:
    items = list(dict.fromkeys(values))
    shown = ", ".join(items[:_MAX_NAMED_IN_REASON])
    extra = len(items) - _MAX_NAMED_IN_REASON
    return f"{shown} and {extra} more" if extra > 0 else shown


# Reasons that mean "the facts to judge this were not available": neutral in the
# reply (like a requirement with no checker), never a failure notice. The last
# two come from requirements with no checker; listing them keeps `is_judged`
# right even if `_has_checker` and `check_requirement` drift apart.
_CANT_CHECK_BEATS = "I can't check the pop-ins on this draft yet."
_CANT_CHECK_SPEECH = "I can't check the spoken parts on this draft yet."
_CANT_CHECK_TAKE = "I can't confirm this draft keeps your whole take."
_CANT_CHECK_TITLE = "I can't confirm where this draft's title came from."
_NO_TITLE = "I didn't add a title because no creator text or grounded brief facts were available."
NO_TITLE_REASON = _NO_TITLE  # stored on a blocked render's receipts (see `render_block_recovery`)
_CANT_CONFIRM_LENGTH = "I can't confirm this draft's length yet."
_TALKING_KEEPS_WHOLE_TAKE = "A Talking edit keeps your whole take, so its length follows your clip"
_VOICEOVER_SETS_LENGTH = "A voiceover edit runs as long as your voiceover"
_CANT_CHECK_TIMING = "I can't verify this timing automatically."
_CANT_CHECK_CLEANUP = "I can't check the speech cleanup on this draft yet."
_CANT_CHECK_CAPTIONS = "I can't check the captions on this draft yet."
_NO_CHECKER = "I can't verify this one automatically yet."
# Speech cleanup (KRI-467 follow-up): what the chosen format does with the ask.
_CLEANUP_PLANNED = "Speech cleanup cuts the long pauses"
_CLEANUP_AT_APPROVAL = "Choose Clean up speech when you approve and the long pauses are cut"
_CLEANUP_IF_OFFERED = (
    "If Clean up speech is offered when you approve, choose it to cut the long pauses"
)
_CLEANUP_UNAVAILABLE = "Speech cleanup isn't available for this project yet, so the pauses stay"
_NAMED_CUTS_NEED_EDITOR = (
    "a retake or a specific line isn't cut automatically yet, so trim that in the editor"
)
_CAPTIONS_WORD_BY_WORD = "words light up as you say them"
_NEUTRAL_REASONS = frozenset(
    {
        _CANT_CHECK_BEATS,
        _CANT_CHECK_SPEECH,
        _CANT_CHECK_TAKE,
        _CANT_CHECK_TITLE,
        _CANT_CHECK_EDITOR_CLIP_TEXT,
        _CANT_CHECK_TARGET_CLIP_TEXT,
        _CANT_CONFIRM_ORDER,
        _CANT_CHECK_ORDER_RULE,
        _CANT_CONFIRM_LENGTH,
        _CANT_CHECK_TIMING,
        _CANT_CHECK_CLEANUP,
        _CANT_CHECK_CAPTIONS,
        _NO_CHECKER,
    }
)

# Reasons that describe what the format the creator chose does with an ask, not a
# simplification Kria made instead of it: a Talking edit's length follows the take,
# speech cleanup is chosen at approval and cuts pauses, never a named line. The
# receipt stays an honest "Partly"; it never turns a first draft into the "should I
# make a simpler version?" question (`needs_creator_choice`), which only makes sense
# when there is a different, simpler plan to choose.
_FORMAT_LIMIT_REASON_PREFIXES: tuple[str, ...] = (
    _TALKING_KEEPS_WHOLE_TAKE,
    _VOICEOVER_SETS_LENGTH,
    _CLEANUP_PLANNED,
    _CLEANUP_AT_APPROVAL,
    _CLEANUP_IF_OFFERED,
    _CLEANUP_UNAVAILABLE,
)


def is_format_limit(reason: str | None) -> bool:
    """True for a receipt reason the creator cannot plan around (see above)."""
    return bool(reason) and any(
        str(reason).startswith(prefix) for prefix in _FORMAT_LIMIT_REASON_PREFIXES
    )


def needs_creator_choice(receipt: RequirementReceipt | Mapping[str, Any]) -> bool:
    """A checked receipt the draft fell short on, that the creator should rule on
    before it replaces their work: not met, and not a limit of the chosen format."""
    row = receipt.model_dump() if isinstance(receipt, RequirementReceipt) else receipt
    return (
        row.get("verification") == "checked"
        and row.get("status") != "met"
        and not is_format_limit(row.get("reason"))
    )


# "labels just say X" / "X only" / "sadece X": the creator wants the text to BE the literal,
# not merely contain it.
_EXACT_TEXT_RE = re.compile(
    r"\b(?:just|only|exactly|simply|solely|sadece|yaln[i\u0131]zca|sade)\b", re.IGNORECASE
)


def _wants_exact_text(req: BriefRequirement) -> bool:
    return bool(_EXACT_TEXT_RE.search(req.description or ""))


def _check_whole_take(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A single-clip subtitled edit renders its one clip full-length."""
    if facts.edit_format is None or facts.editor:
        return _receipt(req, "partial", _CANT_CHECK_TAKE)
    if facts.edit_format != "subtitled":
        return _receipt(
            req,
            "partial",
            f"This {facts.edit_format.replace('_', ' ')} edit cuts your footage down; "
            "a Talking edit keeps the whole take.",
        )
    if facts.video_clip_count is None:
        return _receipt(req, "partial", _CANT_CHECK_TAKE)
    if facts.video_clip_count != 1:
        return _receipt(req, "partial", "This edit uses one of your clips, not every take.")
    if facts.speech_cleanup_enabled:
        return _receipt(
            req, "partial", "Speech cleanup is on, so some pauses or retakes may be cut."
        )
    return _receipt(req, "met", None)


def _check_reaction_beats(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Word-triggered pop-ins and the closing shot, from the repaired strategy."""
    wants_beats = _wants_beats(req)
    wants_closing = _wants_closing(req)
    if facts.reaction_beats is None or facts.editor:
        return _receipt(req, "partial", _CANT_CHECK_BEATS)
    beats = facts.reaction_beats
    problems: list[str] = []
    delivered = False
    if wants_beats:
        if not facts.reaction_beats_available:
            problems.append(
                "Photo and sound pop-ins timed to your words aren't available for this edit yet"
            )
        elif not beats:
            problems.append(
                f"I couldn't find the photos or stickers for {_names(facts.dropped_beat_triggers)}"
                if facts.dropped_beat_triggers
                else "This draft has no pop-ins timed to your words"
            )
        else:
            delivered = True
            named = _named_triggers(req)
            missing = [n for n in named if not any(_trigger_heard(n, b.trigger) for b in beats)]
            unresolved = [
                t
                for t in facts.dropped_beat_triggers
                if not any(_trigger_heard(t, b.trigger) for b in beats)
                and not any(_trigger_heard(t, n) for n in missing)
            ]
            if missing:
                problems.append(f"No pop-in for {_names(missing)}")
            if unresolved:
                problems.append(f"I couldn't find the photo or sticker for {_names(unresolved)}")
            text = _req_text(req)
            if _SOUND_RE.search(text) and not facts.beat_sound_triggers:
                problems.append("None of the pop-ins plays a sound")
            if _VISUAL_RE.search(text) and not facts.beat_visual_ids:
                problems.append("None of the pop-ins shows a photo or sticker")
    if wants_closing:
        if facts.closing_visual_id is None:
            problems.append(
                "I couldn't find the closing photo you named"
                if facts.closing_requested
                else "This draft doesn't end on the photo you asked for"
            )
        else:
            delivered = True
            if facts.closing_badge_requested and facts.closing_badge_id is None:
                problems.append("I couldn't find the closing badge you named")
    if not problems:
        return _receipt(req, "met", None)
    status = "partial" if delivered else "not_possible"
    return _receipt(req, status, "; ".join(problems) + ".")


_SPEECH_EXCERPT_RE = re.compile(
    r"\b(?:excerpts?|speech|spoken|my voice|my words|what i (?:say|said|talk)|"
    r"(?:play|use|hear|keep)\s+(?:my|the)\s+(?:\w+\s+){0,3}"
    r"(?:line|lines|sentence|sentences|quote|quotes|"
    r"talking|speech|voice))\b"
)
_SPEECH_OVER_RE = re.compile(
    r"\b(?:over|b-?roll|other (?:footage|clips|videos)|while|on top of|under)\b"
)
_SPEAKER_RE = re.compile(
    r"\b(?:cut to me|show me|back to me|cut to the speaker|me talking|on camera|me saying)\b"
)
_BACK_TO_MONTAGE_RE = re.compile(r"\b(?:back to|return to|then)\b.{0,30}\b(?:montage|cuts|clips)\b")


def _wants_speech(req: BriefRequirement) -> bool:
    return req.kind in _BEAT_KINDS and bool(_SPEECH_EXCERPT_RE.search(_req_text(req)))


def _check_speech_excerpts(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Spoken excerpts grounded, played over other footage where asked, back to the speaker."""
    sections = facts.speech_sections
    if sections is None or facts.editor:
        return _receipt(req, "partial", _CANT_CHECK_SPEECH)
    text = _req_text(req)
    speech = [s for s in sections if s.kind == "speech"]
    problems: list[str] = []
    missing = _names(f"“{q}”" for q in facts.speech_dropped_quotes)
    if not speech:
        return _receipt(
            req,
            "not_possible",
            "None of your lines made it in" + (f" ({missing})" if missing else ""),
        )
    if missing:
        problems.append(f"I couldn't find {missing} in your clip")
    if _SPEECH_OVER_RE.search(text) and not any(s.visual == "cutaways" for s in speech):
        problems.append("Your words never play over the other footage")
    if _SPEAKER_RE.search(text) and not any(s.visual == "speaker" for s in speech):
        problems.append("It never cuts to you talking")
    # "Return to the speaker": a speaker shot that comes AFTER something that is not the speaker.
    first_other = next(
        (i for i, s in enumerate(sections) if s.kind == "montage" or s.visual == "cutaways"), None
    )
    if (
        first_other is not None
        and _SPEAKER_RE.search(text)
        and not any(s.visual == "speaker" for s in sections[first_other + 1 :])
    ):
        problems.append("It doesn't come back to you after the other footage")
    if _BACK_TO_MONTAGE_RE.search(text) and not any(s.kind == "montage" for s in sections):
        problems.append("There are no fast cuts between your lines")
    if not problems:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "; ".join(problems) + ".")


_TEXT_STYLE_RE = re.compile(
    r"\b(text|label|caption|title|font|bold|italic|colou?r|size|shadow|outline|stroke|"
    r"uppercase|lowercase|yellow|red|blue|green|white|black|pink|orange|purple|renk|yaz[i\u0131])",
    re.IGNORECASE,
)


def _check_speech_cleanup(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Pauses, retakes and filler the creator asked to cut, against the draft.

    Speech cleanup (the preflight check the creator confirms at approval) cuts long
    pauses and non-word sounds out of a Talking or voiceover edit; it never cuts a
    spoken line, so a retake or "the bit where I say X" is reported as editor work.
    A length the same sentence names ("keep it under 45 s") follows the cut take.
    Every honest outcome here is a limit of the format, not a simplification.
    """
    fmt = facts.edit_format
    if fmt is None or facts.editor or fmt not in _SPEECH_FORMATS:
        # Only a speech-spined edit is judged here; a montage's cut is its own
        # planner's to report, so the ask stays "can't verify" there, as before.
        return _receipt(req, "partial", _CANT_CHECK_CLEANUP)
    if facts.speech_cleanup_enabled:
        lead, status = _CLEANUP_PLANNED, "met"
    elif facts.speech_cleanup_offered is True:
        lead, status = _CLEANUP_AT_APPROVAL, "partial"
    elif facts.speech_cleanup_offered is False:
        lead, status = _CLEANUP_UNAVAILABLE, "partial"
    else:
        # Unknown whether approval offers the choice: never promise it.
        lead, status = _CLEANUP_IF_OFFERED, "partial"
    notes = [lead]
    if _wants_named_cuts(req):
        notes.append(_NAMED_CUTS_NEED_EDITOR)
        status = "partial"
    if _has_duration_target(req):
        target = float(req.facts["duration_s"])
        notes.append(f"the length follows what's left of your take, so I can't promise {target:g}s")
        status = "partial"
    return _receipt(req, status, "; ".join(notes) if status == "partial" else None)


def _check_captions(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Captions on / off / word-by-word, against the strategy's caption style.

    Only what the style settles is judged: an ask about a caption's look, place or
    language stays "can't check", and so does ``"auto"`` (the item's own style,
    unknown here) for an on/off or word-by-word ask.
    """
    fmt = facts.edit_format
    text = _req_text(req)
    if (
        facts.caption_style is None
        or facts.editor
        or fmt not in _SPEECH_FORMATS
        or _CAPTION_DETAIL_RE.search(text)
    ):
        return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
    style = facts.caption_style
    if _wants_no_captions(req):
        if style == "none":
            return _receipt(req, "met", None)
        if style == "auto":
            return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
        return _receipt(req, "partial", "Captions are still on in this draft.")
    if style == "none":
        return _receipt(req, "partial", "Captions are off in this draft.")
    if _wants_word_captions(req):
        if style in {"karaoke", "kinetic"}:
            return _receipt(req, "met", _CAPTIONS_WORD_BY_WORD)
        if style == "auto":
            return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
        return _receipt(req, "partial", "Captions are on as full sentences, not word by word.")
    return _receipt(req, "met", None)


def _check_style(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    # Compiled editor ops only exist when they changed a text element, so a text-style
    # ask with edited elements in the payload is proven; anything else goes unjudged.
    if facts.editor and facts.editor_text_edited and _TEXT_STYLE_RE.search(_req_text(req)):
        return _receipt(req, "met", None)
    return _receipt(req, "partial", _NO_CHECKER)


def _check_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if _wants_whole_take(req):
        return _check_whole_take(req, facts)
    target = req.facts.get("duration_s")
    if not isinstance(target, (int, float)) or target <= 0:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    if facts.edit_format == "subtitled":
        # KRI-142: the Talking renderers keep the whole take (minus any speech
        # cleanup); `target_duration_s` never trims it, so it can't be "met".
        return _receipt(req, "partial", _TALKING_KEEPS_WHOLE_TAKE)
    if facts.edit_format in NARRATED_EDIT_FORMATS:
        return _receipt(req, "partial", _VOICEOVER_SETS_LENGTH)
    if facts.duration_s is None:
        return _receipt(req, "partial", _CANT_CONFIRM_LENGTH)
    if abs(facts.duration_s - float(target)) <= DURATION_TOLERANCE * float(target):
        return _receipt(req, "met", None)
    # The edit's own length is what is compared: the phone adds its outro after the edit,
    # so the file runs `outro_s` longer than this (28.0s of edit is a ~29.6s video).
    outro = f" (plus a {facts.outro_s:g}s outro on the finished video)" if facts.outro_s else ""
    return _receipt(
        req,
        "partial",
        f"This draft is about {facts.duration_s:g}s{outro}; you asked for {float(target):g}s.",
    )


_TITLE_SOURCES_THE_CREATOR_OWNS = frozenset({"creator", "brief"})


def _check_title(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A title requirement with no exact text: met when the title is the creator's own
    words or was written from what their brief said (never a default or a model hook)."""
    if not facts.title:
        return _receipt(req, "partial", _NO_TITLE)
    if facts.title_source is None:
        return _receipt(req, "partial", _CANT_CHECK_TITLE)
    if facts.title_source in _TITLE_SOURCES_THE_CREATOR_OWNS:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "I used a plain default title because none was given.")


# The render-time block of the unified phone montage (`_run_phone_unified_montage_job`):
# no video exists when a checked receipt is not met, so the creator-facing copy must not
# read like a draft summary ("I didn't add a title ...") and a title with no words has one
# typed way forward.
NO_TITLE_BLOCKED = "I couldn't make the video yet: you asked for a title but gave no words."
TITLE_WORDS_ALTERNATIVE = 'Tell me the words for the title, or say "continue without a title".'
_TITLE_WAY_FORWARD_MIXED = 'For the title, tell me the words or say "continue without a title".'
_RENDER_BLOCK_SUFFIX = "Your draft is saved. Should I try again or simplify this request?"


@dataclass(frozen=True)
class RenderBlockRecovery:
    """What the creator is told when checked receipts block a render, and the typed decline."""

    message: str
    # Set only when every blocker is one the creator resolves with a single answer.
    decline_reason: str | None = None
    field_path: str | None = None
    alternative: str | None = None


def render_block_recovery(failures: Sequence[Mapping[str, Any]]) -> RenderBlockRecovery:
    """Creator copy (+ typed decline) for the receipts that block a unified-montage render.

    A title with no words is the one blocker the creator answers directly (a
    ``needs_choice`` decline whose alternative is the typed way forward); every other
    blocker keeps the generic "try again or simplify" copy, untyped. Receipt semantics
    are untouched: only the wording shown at the block changes.
    """

    reasons = [str(row.get("reason") or "A requested change is missing.") for row in failures]
    if reasons and all(reason == _NO_TITLE for reason in reasons):
        return RenderBlockRecovery(
            message=f"{NO_TITLE_BLOCKED} Your draft is saved. {TITLE_WORDS_ALTERNATIVE}",
            decline_reason="needs_choice",
            field_path="opening_title",
            alternative=TITLE_WORDS_ALTERNATIVE,
        )
    shown = " ".join(dict.fromkeys(NO_TITLE_BLOCKED if r == _NO_TITLE else r for r in reasons))
    message = f"{shown} {_RENDER_BLOCK_SUFFIX}"
    if _NO_TITLE in reasons:
        # Untyped (two blockers), but the title's way forward must not be lost.
        message = f"{message} {_TITLE_WAY_FORWARD_MIXED}"
    return RenderBlockRecovery(message=message)


def _check_literal_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    wanted = _fold(req.literal or "")
    if req.scope == "title" and facts.title:
        # A title written from the brief may add the route to the creator's words
        # ("20k run · Arnavutköy → Eminönü"), so it only has to contain them.
        found = (
            _contains_text(facts.title, wanted)
            if facts.title_source == "brief"
            else _fold(facts.title) == wanted
        )
    else:
        found = any(_contains_text(t, wanted) for t in facts.texts)
    if found:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "That exact text isn't in this draft.")


def _has_checker(req: BriefRequirement) -> bool:
    """True when ``check_requirement`` can actually verify this requirement."""
    if _wants_cleanup(req) or _wants_captions(req):
        return True
    if req.kind == "text":
        return bool(
            req.scope in ("per_clip", "title") or req.scope.startswith("clip:") or req.literal
        )
    if req.kind == "timing":
        # "Fast but readable" has no number to check: that is "can't verify"
        # (neutral in the reply), not a failed requirement. "Keep my whole take"
        # is checkable against the edit format and clip count.
        return _has_duration_target(req) or _wants_whole_take(req)
    if req.kind in _BEAT_KINDS:
        return _wants_beats(req) or _wants_closing(req) or _wants_speech(req)
    return req.kind == "order"


def check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if _wants_cleanup(req) and facts.edit_format in _SPEECH_FORMATS and not facts.editor:
        # "Cut out the long pauses ... keep it under 45 s" on a Talking or voiceover
        # edit: the cleanup ask owns the sentence; the length it names is judged
        # inside it. Elsewhere the sentence takes its kind's usual path.
        return _check_speech_cleanup(req, facts)
    if req.kind == "text":
        if req.scope == "per_clip" or req.scope.startswith("clip:"):
            return _check_per_clip_text(req, facts)
        if req.literal:
            return _check_literal_text(req, facts)
        if req.scope == "title":
            return _check_title(req, facts)
    elif req.kind == "order":
        return _check_order(req, facts)
    elif req.kind == "timing":
        return _check_timing(req, facts)
    elif req.kind == "style" and facts.editor:
        return _check_style(req, facts)
    elif req.kind in _BEAT_KINDS and _wants_speech(req) and facts.speech_sections is not None:
        return _check_speech_excerpts(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_beats(req) or _wants_closing(req)):
        return _check_reaction_beats(req, facts)
    if _wants_captions(req):
        return _check_captions(req, facts)
    return _receipt(req, "partial", _NO_CHECKER)


# KRI-190: requirement kinds the unified montage planner settles at render time. Its
# receipts are built from the plan it actually made, so a draft-time check of these is
# premature: the strategy draft has no per-clip text, order or timing yet, and would
# report "None of the 14 clips got its own text" for a video that then gets 14 labels.
UNIFIED_SETTLED_KINDS = frozenset({"text", "order", "timing"})


# Approval turns `audio_strategy` into the item's audio mode: only these leave the
# voiceover lane, and the worker takes the unified planner only outside it. KRI-374:
# `user_song` (audio_mode "song") also runs through the unified montage planner.
_NON_VOICEOVER_AUDIO = frozenset({"original_audio", "licensed_music", "user_song"})


def defers_to_unified_montage(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft will render through the unified phone montage planner.

    Mirrors the worker's own choice so the two cannot leave a requirement judged nowhere:
    a phone job (some clip is a phone analysis proxy and the account is enrolled), a
    montage-family format, and no voiceover lane (KRI-220: no flag). Approval
    maps `audio_strategy` to the audio mode (`original_audio`/`licensed_music` leave the
    voiceover lane; anything else is the voiceover lane or a `voiceover_required` refusal),
    so an absent or voiceover-ish strategy is conservatively not deferred.
    """
    from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415

    return bool(
        str(audio_strategy or "") in _NON_VOICEOVER_AUDIO
        and str(edit_format or "") in GUIDED_EDIT_FORMATS
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
    )


def requirements_to_check_at_draft(
    requirements: Iterable[BriefRequirement],
    *,
    creator_id: object,
    strategy: Mapping[str, Any] | None,
    item_edit_format: object,
    clip_paths: Iterable[object] = (),
) -> list[BriefRequirement]:
    """The requirements a strategy draft can honestly be checked against.

    When the unified planner will settle a requirement at render time (`text`, `order`,
    `timing`) it is left out, so the draft reply is the plain summary instead of a
    premature failure notice; the render's own receipts (met / partial / not possible,
    plus guessed names) follow. Otherwise this is the unchanged, full list.
    """
    strategy = strategy or {}
    defers = defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=strategy.get("edit_format") or item_edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    return [req for req in requirements if not (defers and req.kind in UNIFIED_SETTLED_KINDS)]


def is_judged(req: BriefRequirement | None, receipt: RequirementReceipt) -> bool:
    """True when a checker had what it needed to decide ``req``.

    With no checker, or with a neutral reason (the facts were missing), nothing
    was checked. Such a receipt read "Partly: add captions (I can't verify this
    one automatically yet)", a half-done claim on nearly every reply. Receipts
    stored before these stopped being written still exist, so every reader of
    stored receipts filters through this too.
    """
    return (
        req is not None
        and receipt.verification != "unchecked"
        and _has_checker(req)
        and receipt.reason not in _NEUTRAL_REASONS
    )


def build_receipts(
    requirements: Iterable[BriefRequirement],
    facts: PlanFacts,
    *,
    include_unchecked: bool = False,
    strict_order: bool | None = None,
) -> list[RequirementReceipt]:
    """Build receipts without changing legacy omission or serialization by default.

    Bound writers opt in with ``include_unchecked=True``. That emits every
    live requirement: determinate receipts are ``checked`` at the ``checked``
    stage, while unavailable evidence is ``unchecked`` at ``understood`` (or
    ``matched`` when a clip target is known).

    ``strict_order`` (default: ``include_unchecked``, i.e. the writer is bound) turns on the
    stricter verdicts for a required order (see ``_check_order``). A contract-stamped job
    passes it explicitly; a legacy / unbound job keeps the original semantics.
    """
    if (include_unchecked if strict_order is None else strict_order) and not facts.strict_order:
        facts = dataclasses.replace(facts, strict_order=True)
    checked = ((req, check_requirement(req, facts)) for req in requirements if req.live)
    receipts: list[RequirementReceipt] = []
    for req, receipt in checked:
        if is_judged(req, receipt):
            receipts.append(
                receipt.model_copy(update={"verification": "checked", "stage": "checked"})
                if include_unchecked
                else receipt
            )
        elif include_unchecked:
            receipts.append(
                receipt.model_copy(
                    update={
                        "verification": "unchecked",
                        "stage": "matched" if receipt.target_media_ids else "understood",
                    }
                )
            )
    return receipts


# ------------------------------------------------------------------------ reply

_LABEL = {
    "met": "Done",
    "partial": "Partly",
    "not_possible": "Couldn't",
    # KRI-282: the creator was asked and chose the other side of a conflict.
    "chosen": "As you chose",
}


def reply_from_receipts(
    brief: CreativeBrief,
    receipts: list[RequirementReceipt],
    *,
    summary: str | None = None,
    notices: Sequence[str] = (),
    outcomes: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Compose the creator-facing reply from receipts only.

    The model's free-text summary is kept only when every judged requirement is
    met; otherwise the reply is exactly what was checked, so it cannot overclaim.
    ``notices`` are the server's own repair notes (KRI-142). The summary already
    carries them, so they are added back only when the summary is replaced.
    An unjudged receipt (stored before ``build_receipts`` dropped them) gets no
    line and never turns the reply into a failure notice.
    """
    by_id = {req.id: req for req in brief.requirements}
    judged = [r for r in receipts if is_judged(by_id.get(r.requirement_id), r)]
    unchecked = [
        r
        for r in receipts
        if r.verification == "unchecked" and by_id.get(r.requirement_id) is not None
    ]
    lines: list[str] = []
    guesses: list[str] = []
    # KRI-282: what the render did for each requested group / label / chapter text,
    # read from the finished plan, listed whether or not the brief ledger judged it.
    failed = False
    for row in outcomes:
        status = str(row.get("status") or "")
        if status not in _LABEL or not row.get("name"):
            continue
        failed = failed or status not in {"met", "chosen"}
        line = f"{_LABEL[status]}: {row['name']}"
        if row.get("reason"):
            line += f" ({str(row['reason']).rstrip('.')})"
        lines.append(line)
    for receipt in judged:
        line = f"{_LABEL[receipt.status]}: {by_id[receipt.requirement_id].text()}"
        if receipt.reason:
            line += f" ({receipt.reason.rstrip('.')})"
        lines.append(line)
        guesses.extend(receipt.inferred)
    if guesses:
        shown = ", ".join(dict.fromkeys(guesses))
        lines.append(
            f"I guessed these, tell me if any is wrong: {shown} "
            "(text I took from the footage, not from your words)"
        )
    body = "\n".join(f"- {line}" for line in lines)
    if unchecked:
        unchecked_lines = []
        for receipt in unchecked:
            line = f"Couldn't verify: {by_id[receipt.requirement_id].text()}"
            if receipt.reason:
                line += f" ({receipt.reason.rstrip('.')})"
            unchecked_lines.append(f"- {line}")
        text = "I couldn't verify every requested change:\n" + "\n".join(
            [*unchecked_lines, *([body] if body else [])]
        )
        if notices:
            text += "\n" + " ".join(notices)
    elif failed or any(r.status != "met" for r in judged):
        text = "Not everything you asked for made it in:\n" + body
        if notices:
            text += "\n" + " ".join(notices)
    else:
        text = "\n".join(part for part in ((summary or "").strip(), body) if part)
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


__all__ = [
    "UNIFIED_SETTLED_KINDS",
    "BeatFact",
    "NO_TITLE_REASON",
    "RenderBlockRecovery",
    "defers_to_unified_montage",
    "requirements_to_check_at_draft",
    "PlanFacts",
    "build_receipts",
    "check_requirement",
    "is_format_limit",
    "is_judged",
    "needs_creator_choice",
    "SpeechSectionFact",
    "plan_facts_from_editor_payload",
    "plan_facts_from_speech_montage",
    "plan_facts_from_strategy",
    "plan_facts_from_unified_montage",
    "render_block_recovery",
    "reply_from_receipts",
]
