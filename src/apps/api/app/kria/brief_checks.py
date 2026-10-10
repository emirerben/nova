"""Deterministic per-requirement checks and receipt-driven replies (KRI-188).

No model is involved. Each checker compares one Creative Brief requirement with
facts read from the drafted plan (a strategy or an editor payload) and returns a
``RequirementReceipt``. A requirement nothing could judge (no checker, or the
facts to judge it were missing) gets no receipt: it stays ``open`` and the reply
says nothing about it, so the reply never claims what the server did not check
and never labels an unchecked ask "Partly".

Checks implemented: per-clip text coverage, ordering vs the requested key,
duration within +/-10%, literal on-screen text, "keep my whole take" on a
single-clip subtitled edit, word-triggered pop-ins (reaction beats) plus the
closing shot on a phone Talking edit, and (KRI-546) a finished phone montage's
held closing line and repeated video files.
"""

from __future__ import annotations

import dataclasses
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import BriefRequirement, CreativeBrief, normalize_style_intent
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
from app.kria.contracts import InferredLabel, RequirementReceipt
from app.kria.reply_language import current_reply_language, say
from app.kria.style_asks import (
    clip_length_ask,
    derive_style_ask,
    text_look_facts,
    value_matches,
)
from app.schemas.clip_intents import PLACEHOLDER_LABEL_TEXT
from app.schemas.text_style_intent import (
    LABEL_ANCHORS,
    normalize_label_position,
    normalize_title_animation,
)
from app.services.clip_facts import CAPTURE_ORDER_KEYS
from app.services.kria_editor_ops_diff import effective_entrance

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
    # Seconds into the render where the pop-in shows (a rendered variant's receipt);
    # None for a draft, which has not been laid out yet.
    at_s: float | None = None


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
class NarratedStepFact:
    """One clip of a phone Voiceover edit as the worker placed it (KRI-533).

    ``labels`` are the creator's own words for the shot ("the balloons"), ``placed`` the
    first/last seats a resolved order intent gave it, and ``text`` what the voiceover says
    while the clip is on screen.
    """

    media_id: str = ""
    labels: tuple[str, ...] = ()
    placed: tuple[tuple[str, str], ...] = ()  # (spot, name)
    start_s: float = 0.0
    end_s: float = 0.0
    text: str = ""


@dataclass(frozen=True)
class TextStyleRow:
    """One non-caption on-screen text row's style as saved (KRI-543).

    ``kind`` is the editor's own title/label/text classification. A style field is None
    when the saved row does not say (an unset font or color falls back to a renderer
    default we do not read), so a check on it stays "can't verify" instead of guessing.
    """

    id: str
    kind: str = "text"
    entrance: str | None = None
    alignment: str | None = None
    text_case: str | None = None
    font_family: str | None = None
    color: str | None = None
    # KRI-558: where the row sits and what it says, so a label-corner or "the Lisbon text"
    # ask can be judged. ``position`` is the named spot; x/y only mean something for "custom".
    size_px: float | None = None
    position: str | None = None
    x_frac: float | None = None
    y_frac: float | None = None
    clip_id: str | None = None
    text: str = ""


@dataclass(frozen=True)
class ClosingSpeechFact:
    """The spoken line a finished phone montage holds whole on its closing clip (KRI-546).

    Read off the plan record (`unified_montage.closing_speech`, recorded only when the
    creator's own "last" ask seated the clip, it speaks and the camera audio is kept) and
    confirmed against the finished timeline: the clip is last and its cut covers the line.
    """

    media_id: str
    start_s: float
    end_s: float


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
    # KRI-541: True when the facts were read off a finished phone render
    # (`plan_facts_from_phone_variant`). `editor` stays True for its text lanes, but the
    # edit format, caption style and speech cleanup below are what the render did, so the
    # captions and cleanup checkers judge them instead of answering "can't check".
    rendered_variant: bool = False
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
    # Compiled text spans, when the editor payload includes timing and identity.
    # Each row is (element id, role, start_s, end_s); an empty tuple means timing
    # was not available and must remain unchecked.
    text_spans: tuple[tuple[str, str, float, float], ...] = ()
    text_timing_incomplete: bool = False
    # Per-row style of the saved non-caption text rows (KRI-543). None means the text lane
    # was not available (a draft or render), which keeps a style ask unchecked.
    text_styles: tuple[TextStyleRow, ...] | None = None
    # KRI-558: on an editor turn, the ids of the text rows the creator's ops changed. A style
    # ask that names no target ("…to all of them") is judged on exactly these rows. None =
    # not an editor turn (a draft or a render judges every row).
    anaphora_rows: tuple[str, ...] | None = None
    # KRI-558: each clip's output length in screen order (removed clips left out). None =
    # unknown, which keeps a per-clip length ask unjudged (never a false failure).
    clip_output_durations: tuple[float, ...] | None = None
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
    # What speech cleanup did on a rendered variant: "applied" (pauses were cut),
    # "no_change" (it ran and found nothing to cut), "not_run" (the render kept the whole
    # take). None = unknown or a draft.
    speech_cleanup_outcome: str | None = None
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
    # KRI-537: triggers the render never heard in the creator's voice (a phone variant's
    # `phone_beat_receipt.unplaced[]`), so "never said" is told apart from "no room".
    unheard_beat_triggers: tuple[str, ...] = ()
    # The strategy's audio strategy ("voiceover", "original_audio", ...); None = unknown.
    audio_strategy: str | None = None
    # How loud the footage's own sound plays under the creator's voice (0..1; 1.0 = full
    # volume). None = unknown, never guessed.
    voiceover_bed_level: float | None = None
    closing_requested: bool = False
    closing_visual_id: str | None = None
    closing_badge_requested: bool = False
    closing_badge_id: str | None = None
    # KRI-282: the spoken-excerpt montage as the compiler laid it out, in order.
    # None = this plan is not a spoken-excerpt montage (nothing about speech is claimed).
    speech_sections: tuple[SpeechSectionFact, ...] | None = None
    # Quotes the planner chose that were not found in the clip's speech.
    speech_dropped_quotes: tuple[str, ...] = ()
    # KRI-533: a phone Voiceover edit as the worker laid it out, clips in screen order.
    # None = this plan is not a rendered Voiceover edit (nothing about it is claimed).
    narrated_steps: tuple[NarratedStepFact, ...] | None = None
    # The language the rendered captions are in, and the language that was spoken.
    caption_language: str | None = None
    spoken_language: str | None = None
    # KRI-549: the caption lines a finished phone render shows (its `caption_cues` text, in
    # order); () = it shows none. None = unknown (a draft, an editor edit, a cloud render).
    caption_texts: tuple[str, ...] | None = None
    # KRI-546: a finished phone unified montage, judged from its plan record and the render
    # together (`plan_facts_from_rendered_montage`). Only there do the closing-line and
    # duplicate-video checks below judge anything; every other plan keeps today's answers.
    rendered_montage: bool = False
    # The closing clip's whole spoken line, held by the plan and confirmed on the finished
    # timeline. None = no line was held (or the render does not show it held).
    closing_speech: ClosingSpeechFact | None = None
    # False when the render records that the clips' own sound was dropped; None = unknown.
    source_audio_kept: bool | None = None
    # Every clip the creator added to this edit (footage and Visuals), whether or not the
    # edit used it, so "that clip isn't in the edit" is told apart from an unknown clip id.
    source_clip_ids: tuple[str, ...] = ()
    # The finished edit's clips (1-based, in screen order) that play the same original file
    # (same upload fingerprint), one group per file shown more than once; and how many extra
    # copies the creator's own uploads held. None = some clip had no fingerprint, so
    # nothing about duplicates is claimed.
    duplicate_clip_positions: tuple[tuple[int, ...], ...] | None = None
    source_duplicate_copies: int | None = None

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


def _pinned_text_values(pins: object) -> list[str]:
    """KRI-523: the exact lines of whole-video corner text (strategy or unified record)."""
    return [
        str(pin["text"])
        for pin in (pins if isinstance(pins, list) else [])
        if isinstance(pin, Mapping) and pin.get("text")
    ]


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
            *_pinned_text_values(strategy.get("pinned_texts")),
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
    audio_strategy = strategy.get("audio_strategy")
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
        audio_strategy=str(audio_strategy) if audio_strategy else None,
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


_SPOT_TR = {"first": "ilk", "last": "son", "then": "sonra"}
# What an ordering basis / edit format is called in Turkish copy; an unknown value
# is shown as written (underscores spaced), exactly as the English copy does.
_BASIS_TR = {
    "attachment": "klipleri eklediğin sıra",
    "attachment_order": "klipleri eklediğin sıra",
    "creator_order": "senin verdiğin sıra",
    "song_time": "şarkının zamanı",
    "confirmed": "onayladığın sıra",
    "editor": "editördeki sıra",
    "route": "rota",
}
_FORMAT_TR = {
    "montage": "montaj",
    "day_vlog": "günlük vlog",
    "single_hero": "tek kahraman",
    "talking_head": "konuşmalı",
    "subtitled": "konuşmalı",
    "narrated": "seslendirmeli",
    "narrated_planned": "seslendirmeli",
    "narrated_ready": "seslendirmeli",
    "slides": "slayt",
}


def _sequence_label(name: str) -> str:
    """``first: the video that is blue`` (the planner's outcome name) -> ``the video that is
    blue (first)``, the way a creator would say it back."""
    spot, _, words = name.partition(": ")
    if words and spot in ("first", "last", "then"):
        return say(en=f"{words} ({spot})", tr=f"{words} ({_SPOT_TR[spot]})")
    return name or say(en="the order you described", tr="tarif ettiğin sıra")


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
            str(text)
            for text in (
                title,
                record.get("closing_title"),
                *per_clip.values(),
                *_pinned_text_values(record.get("pinned_texts")),
            )
            if text
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
        # KRI-527: corner text the writer actually drew (a pin with no window is not claimed).
        texts=tuple(_pinned_text_values(record.get("pinned_texts"))),
        rendered_output=True,
    )


# Where the phone Voiceover worker keeps the evidence behind these receipts.
NARRATED_ALIGNMENT_FIELD = "narrated_alignment"

# Orderings whose clip times were set by the creator's words (a scripted guide or the
# alignment agent), so "while I talk about X" can be read off the step windows.
NARRATED_ALIGNED_BASES = frozenset({"spoken_word_alignment", "guide_script_alignment"})


def plan_facts_from_narrated_alignment(record: Mapping[str, Any] | None) -> PlanFacts:
    """Read verifiable facts off a phone Voiceover render record (KRI-533).

    ``record`` is ``assembly_plan["narrated_alignment"]``: the steps the worker pinned
    (each clip's creator labels, window and spoken words) and the caption language it
    actually burned, never what a model claimed.
    """
    record = record or {}
    steps: list[NarratedStepFact] = []
    for row in record.get("steps") or []:
        if not isinstance(row, Mapping):
            continue
        placed = tuple(
            (str(p["spot"]), str(p["name"]))
            for p in row.get("placed") or []
            if isinstance(p, Mapping) and p.get("spot") and p.get("name")
        )
        start, end = row.get("start_s"), row.get("end_s")
        steps.append(
            NarratedStepFact(
                media_id=str(row.get("media_id") or ""),
                labels=tuple(str(x) for x in row.get("labels") or [] if str(x).strip()),
                placed=placed,
                start_s=float(start) if isinstance(start, (int, float)) else 0.0,
                end_s=float(end) if isinstance(end, (int, float)) else 0.0,
                text=" ".join(str(row.get("text") or "").split()),
            )
        )
    basis = record.get("ordering_basis")
    language = record.get("caption_language")
    spoken = record.get("spoken_language")
    return PlanFacts(
        clip_ids=tuple(step.media_id for step in steps),
        ordering_basis=str(basis) if basis else None,
        rendered_output=True,
        narrated_steps=tuple(steps),
        caption_language=str(language) if language else None,
        spoken_language=str(spoken) if spoken else None,
    )


def _editor_payload_duration(payload: Mapping[str, Any]) -> float | None:
    """Output length implied by the payload's timeline slots, or None when unprovable.

    Only exact-second slots count (beat-sized slots need the audio grid) and a
    speed change divides the window. Crossfade overlap is ignored, which the
    tolerance absorbs.
    """
    slots = payload.get("timeline_slots") if isinstance(payload, Mapping) else None
    if not isinstance(slots, list) or not slots:
        for key in ("edit_duration_s", "total_duration_s", "duration_s"):
            duration = payload.get(key)
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(float(duration))
                and duration > 0
            ):
                return float(duration)
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


_LEGACY_EFFECT_ENTRANCE = {
    "static": "none",
    "none": "none",
    "fade-in": "fade",
    "pop-in": "pop",
    "slide-in": "slide",
    "typewriter": "typewriter",
}
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ENTRANCES = frozenset({"none", "fade", "pop", "slide", "typewriter"})
_TEXT_CASES = frozenset({"none", "upper", "lower", "title"})
_ALIGNMENTS = frozenset({"left", "center", "right"})


def _row_entrance(row: Mapping[str, Any]) -> str | None:
    """The entrance a saved row plays: explicit phases win, else the legacy effect's."""
    entrance = effective_entrance(row)
    return entrance if entrance in _ENTRANCES else None


def _row_choice(
    row: Mapping[str, Any], key: str, allowed: frozenset[str], default: str
) -> str | None:
    value = row.get(key)
    if value is None:
        return default
    return value if value in allowed else None


def _row_clip_id(row: Mapping[str, Any]) -> str | None:
    """The clip a label row belongs to: its ``clip_id``, else the media id in its row id."""
    clip = row.get("clip_id")
    if isinstance(clip, str) and clip:
        return clip
    row_id = str(row.get("id") or "")
    prefix = "clip-label-media-"
    return row_id[len(prefix) :] or None if row_id.startswith(prefix) else None


def _text_style_rows(rows: Iterable[Any]) -> tuple[TextStyleRow, ...]:
    """Style of the editor's live, non-caption text rows, typed by the editor's own classifier."""
    from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415
    from app.services.kria_editor_ops_text import classify  # noqa: PLC0415

    live = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("text"), str)
        and row["text"].strip()
        and not row.get("removed")
        and row.get("role") != "lyric_line"
        and not is_caption_text_bar(dict(row))
    ]
    bars = []
    for index, row in enumerate(live):
        params = row.get("source_params")
        source = row.get("sequence_source_id")
        if not isinstance(source, str) and isinstance(params, Mapping):
            source = params.get("sequence_source_id")
        clip = row.get("clip_id")
        start = row.get("start_s")
        bars.append(
            {
                "index": index,
                "id": row["id"],
                "text": str(row.get("text") or ""),
                "role": row.get("role"),
                "sequence_source_id": source if isinstance(source, str) else None,
                "clip_id": clip if isinstance(clip, str) and clip else None,
                "removed": False,
                "start_s": float(start)
                if isinstance(start, (int, float)) and not isinstance(start, bool)
                else None,
                "caption": False,
            }
        )
    kinds = classify(bars)
    out = []
    for row in live:
        color = row.get("color")
        font = row.get("font_family")
        out.append(
            TextStyleRow(
                id=row["id"],
                kind=kinds.get(row["id"], "text"),
                entrance=_row_entrance(row),
                alignment=_row_choice(row, "alignment", _ALIGNMENTS, "center"),
                text_case=_row_choice(row, "text_case", _TEXT_CASES, "none"),
                font_family=font if isinstance(font, str) and font else None,
                color=color.upper() if isinstance(color, str) and _HEX_COLOR.match(color) else None,
                size_px=_finite_number(row.get("size_px")),
                position=row.get("position") if isinstance(row.get("position"), str) else None,
                x_frac=_finite_number(row.get("x_frac")),
                y_frac=_finite_number(row.get("y_frac")),
                clip_id=_row_clip_id(row),
                text=str(row.get("text") or ""),
            )
        )
    return tuple(out)


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
    text_spans: list[tuple[str, str, float, float]] = []
    text_timing_incomplete = False

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
            row_id = row.get("id")
            start, end = row.get("start_s"), row.get("end_s")
            if (
                isinstance(row_id, str)
                and isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and not isinstance(start, bool)
                and not isinstance(end, bool)
                and math.isfinite(float(start))
                and math.isfinite(float(end))
                and end > start >= 0
            ):
                text_spans.append(
                    (row_id, str(row.get("role") or "text"), float(start), float(end))
                )
            elif isinstance(row_id, str) and str(row.get("role") or "text") in {
                "title",
                "generative_intro",
            }:
                text_timing_incomplete = True
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
        text_spans=tuple(text_spans),
        text_timing_incomplete=text_timing_incomplete,
        text_styles=(
            _text_style_rows(payload["text_elements"])
            if isinstance(payload, Mapping) and isinstance(payload.get("text_elements"), list)
            else None
        ),
        clip_output_durations=_editor_clip_durations(payload),
    )


def _editor_clip_durations(payload: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Each kept clip's output length in order, or None when any slot cannot say (KRI-558)."""
    slots = payload.get("timeline_slots") if isinstance(payload, Mapping) else None
    if not isinstance(slots, list) or not slots:
        return None
    out: list[float] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            return None
        if slot.get("removed"):
            continue
        duration = _finite_number(slot.get("duration_s"))
        if duration is None or duration <= 0:
            return None
        rate = _finite_number(slot.get("playback_rate"))
        out.append(duration / (rate if rate and rate > 0 else 1.0))
    return tuple(out) or None


def _rendered_clip_durations(variant: Mapping[str, Any]) -> tuple[float, ...] | None:
    """Each main-picture clip's length on the finished timeline, in screen order."""
    rows = variant.get("story_timeline")
    spans: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("lane", "clip") != "clip":
            continue
        start, end = (
            _finite_number(row.get("output_start_s")),
            _finite_number(row.get("output_end_s")),
        )
        if start is None or end is None or end <= start:
            return None
        spans.append((start, end - start))
    return tuple(length for _start, length in sorted(spans)) or None


# Receipt reasons that mean the spoken trigger itself never played in the creator's voice
# (as opposed to "heard, but no safe room to show it"): see `beat_miss_sentence`.
_NEVER_HEARD_REASONS = frozenset({"never_heard", "after_not_heard", "no_spoken_match"})


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _phone_receipt_facts(receipt: object, *, timed: bool) -> dict[str, Any]:
    """Beat and closing facts read off a variant's persisted ``phone_beat_receipt``.

    ``timed`` says the receipt's ``at_s`` is on the output timeline. A Talking render remaps
    its lanes with the cut plan but leaves the receipt on the source take's timeline, so
    only a Voiceover render (whose timeline is the voice) can quote times.
    """
    # A manual lane, a missing matcher or a failed matcher says nothing about the pop-ins.
    if not isinstance(receipt, Mapping) or receipt.get("matcher") in ("manual", "failed", None):
        return {}
    beats: list[BeatFact] = []
    placed = receipt.get("placed")
    for entry in placed if isinstance(placed, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("trigger"), str):
            continue
        at_s = _finite_number(entry.get("at_s")) if timed else None
        beats.append(
            BeatFact(
                trigger=entry["trigger"],
                visual_id=str(entry.get("visual_label") or "") or None,
                sound=str(entry.get("sound_label") or "") or None,
                at_s=at_s,
            )
        )
    unheard: list[str] = []
    unplaced = receipt.get("unplaced")
    for entry in unplaced if isinstance(unplaced, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("trigger"), str):
            continue
        reason = entry.get("reason")
        if not reason or reason in _NEVER_HEARD_REASONS:
            unheard.append(entry["trigger"])
    facts: dict[str, Any] = {
        "reaction_beats_available": True,
        "reaction_beats": tuple(beats),
        "unheard_beat_triggers": tuple(dict.fromkeys(unheard)),
    }
    closing = receipt.get("closing")
    if isinstance(closing, Mapping):
        if closing.get("status") == "placed":
            facts["closing_visual_id"] = str(closing.get("visual_label") or "") or "closing"
        elif closing.get("status") == "unplaced":
            facts["closing_requested"] = True
        if closing.get("badge") == "placed":
            facts["closing_badge_requested"] = True
            facts["closing_badge_id"] = "badge"
        elif closing.get("badge") == "unplaced":
            facts["closing_badge_requested"] = True
    return facts


def plan_facts_from_phone_variant(variant: Mapping[str, Any] | None) -> PlanFacts:
    """Facts off a rendered phone variant (KRI-537).

    Starts from the editor-payload facts (on-screen text lanes, ``editor=True``) and adds
    what the render itself recorded: the ``phone_beat_receipt`` (where each pop-in landed,
    which words were never heard, the closing shot) and ``voiceover_bed_level`` (how loud
    the footage's own sound plays under the voice). Anything the variant does not carry
    stays unknown; nothing is guessed.
    """
    if not isinstance(variant, Mapping):
        return PlanFacts()
    base = plan_facts_from_editor_payload(variant)
    changes = _phone_receipt_facts(
        variant.get("phone_beat_receipt"), timed=variant.get("resolved_archetype") == "narrated"
    )
    bed = _finite_number(variant.get("voiceover_bed_level"))
    if bed is not None:
        changes["voiceover_bed_level"] = max(0.0, bed)
        changes["audio_strategy"] = "voiceover"
    elif variant.get("resolved_archetype") == "narrated":
        changes["audio_strategy"] = "voiceover"
    changes.update(_rendered_speech_facts(variant))
    changes.update(_rendered_caption_facts(variant))
    rendered = _rendered_clip_durations(variant)
    if rendered is not None:
        changes["clip_output_durations"] = rendered
    return dataclasses.replace(base, **changes) if changes else base


# KRI-541: the speech-spined archetypes a phone render records, as the brief's edit format.
# A multi-clip phone Talking edit keeps `resolved_archetype == "subtitled"` too.
_RENDERED_EDIT_FORMATS = {"narrated": "narrated", "subtitled": "subtitled"}


def _rendered_speech_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """Edit format, caption style and speech cleanup off a rendered Voiceover/Talking variant.

    Both phone writers persist ``caption_cues``, ``voiceover_caption_style`` ("sentence"
    or "word") and, only when cleanup ran, ``silence_cut_outcome``. A device render
    always writes its cues, so no cues there means no captions and no outcome means the
    take was kept whole; a cloud render fills those fields later, so their absence stays
    unknown. Any other archetype adds nothing (the checkers keep today's answers).
    """
    edit_format = _RENDERED_EDIT_FORMATS.get(str(variant.get("resolved_archetype") or ""))
    if edit_format is None:
        return {}
    device = variant.get("render_destination") == "device"
    facts: dict[str, Any] = {"rendered_variant": True, "edit_format": edit_format}
    cues = variant.get("caption_cues")
    if isinstance(cues, list) and cues:
        facts["caption_style"] = (
            "karaoke" if variant.get("voiceover_caption_style") == "word" else "clean"
        )
    elif device:
        facts["caption_style"] = "none"
    outcome = variant.get("silence_cut_outcome")
    if outcome in ("applied", "no_change"):
        facts["speech_cleanup_enabled"] = True
        facts["speech_cleanup_outcome"] = outcome
    elif outcome is None and device:
        facts["speech_cleanup_enabled"] = False
        facts["speech_cleanup_outcome"] = "not_run"
    return facts


def _rendered_caption_facts(variant: Mapping[str, Any]) -> dict[str, Any]:
    """The caption lines and their language off a rendered Voiceover/Talking variant (KRI-549).

    Both phone writers persist ``caption_cues`` (one ``text`` per line) and
    ``caption_language``. A device render with no cues shows no captions; a cloud render
    fills them later, so their absence stays unknown. Captions the creator turned off
    (``captions_enabled: false``) are not on screen, so nothing is read from them.
    """
    if _RENDERED_EDIT_FORMATS.get(str(variant.get("resolved_archetype") or "")) is None:
        return {}
    if variant.get("captions_enabled") is False:
        return {}
    cues = variant.get("caption_cues")
    texts = tuple(
        " ".join(str(cue.get("text") or "").split())
        for cue in (cues if isinstance(cues, list) else ())
        if isinstance(cue, Mapping)
    )
    texts = tuple(text for text in texts if text)
    if not texts:
        return {"caption_texts": ()} if variant.get("render_destination") == "device" else {}
    facts: dict[str, Any] = {"caption_texts": texts}
    language = variant.get("caption_language")
    if isinstance(language, str) and language.strip():
        facts["caption_language"] = language.strip()
    return facts


# KRI-546: the order facts a unified montage's plan record holds, laid over a finished
# variant's facts so an order ask is judged at render-ready exactly as the plan was.
_MONTAGE_ORDER_FIELDS = (
    "ordering_basis",
    "ordering_fallback_clip_ids",
    "ordering_choice",
    "sequence_statuses",
    "sequence_unmet",
    "sequence_absent",
    "sequence_spots_met",
    "route_start",
    "route_end",
    "first_endpoint",
    "last_endpoint",
)
# How far a finished cut may start after / end before the held line and still hold it: the
# timeline is rounded to milliseconds and one frame at 30 fps is ~0.033 s.
_HELD_LINE_TOLERANCE_S = 0.05


def _rendered_clip_cuts(
    variant: Mapping[str, Any],
) -> list[tuple[str, float | None, float | None]]:
    """The finished edit's main-picture cuts in screen order: (media id, source start, end)."""
    rows = variant.get("story_timeline")
    cuts: list[tuple[float, int, str, float | None, float | None]] = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, Mapping) or row.get("lane", "clip") != "clip":
            continue
        media_id = row.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            continue
        at = _finite_number(row.get("output_start_s"))
        cuts.append(
            (
                at if at is not None else math.inf,
                index,
                media_id,
                _finite_number(row.get("source_start_s")),
                _finite_number(row.get("source_end_s")),
            )
        )
    cuts.sort(key=lambda cut: (cut[0], cut[1]))
    return [(media_id, start, end) for _at, _index, media_id, start, end in cuts]


def _same_file_positions(
    ids: Sequence[str], fingerprints: Mapping[str, str]
) -> tuple[tuple[int, ...], ...] | None:
    """1-based positions in ``ids`` that play the same original file, one group per file
    shown more than once (two cuts of one clip count too); None when a clip is unhashed."""
    if not ids or any(not fingerprints.get(media_id) for media_id in ids):
        return None
    by_file: dict[str, list[int]] = {}
    for position, media_id in enumerate(ids, start=1):
        by_file.setdefault(fingerprints[media_id], []).append(position)
    return tuple(tuple(group) for group in by_file.values() if len(group) > 1)


def _held_closing_line(
    record: Mapping[str, Any],
    clip_ids: Sequence[str],
    last_cut: tuple[str, float | None, float | None] | None,
) -> ClosingSpeechFact | None:
    """The record's held closing line when the finished edit really ends on it, else None."""
    speech = record.get("closing_speech")
    if not isinstance(speech, Mapping) or not clip_ids:
        return None
    media_id = str(speech.get("media_id") or "")
    start = _finite_number(speech.get("source_start_s"))
    end = _finite_number(speech.get("source_end_s"))
    if not media_id or start is None or end is None or end <= start or clip_ids[-1] != media_id:
        return None
    if last_cut is not None:
        # A finished timeline must show the closing cut covering the whole line.
        cut_start, cut_end = last_cut[1], last_cut[2]
        if (
            cut_start is None
            or cut_end is None
            or cut_start > start + _HELD_LINE_TOLERANCE_S
            or cut_end < end - _HELD_LINE_TOLERANCE_S
        ):
            return None
    return ClosingSpeechFact(media_id=media_id, start_s=start, end_s=end)


def plan_facts_from_rendered_montage(
    variant: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
    *,
    fingerprints: Mapping[str, str] | None = None,
) -> PlanFacts:
    """Facts off a finished phone unified montage (KRI-546).

    The variant's own facts (`plan_facts_from_phone_variant`: on-screen text, length) plus
    what only the plan record and the finished timeline together show: how the clips were
    ordered, whether the closing clip's spoken line plays whole in its own sound, and which
    clips came from the same original file. ``fingerprints`` maps each media id the creator
    added to the sha256 of its original upload; without it nothing about duplicates is
    claimed. Pass only the record of the generation this variant rendered.
    """
    facts = plan_facts_from_phone_variant(variant)
    if not isinstance(variant, Mapping) or not isinstance(record, Mapping):
        return facts
    plan = plan_facts_from_unified_montage(record)
    cuts = _rendered_clip_cuts(variant)
    clip_ids = tuple(media_id for media_id, _start, _end in cuts) or plan.clip_ids
    prints = {
        str(media_id): str(sha)
        for media_id, sha in (fingerprints or {}).items()
        if isinstance(media_id, str) and media_id and isinstance(sha, str) and sha
    }
    kept = variant.get("source_audio_preserved")
    return dataclasses.replace(
        facts,
        **{name: getattr(plan, name) for name in _MONTAGE_ORDER_FIELDS},
        clip_ids=clip_ids,
        rendered_output=True,
        rendered_montage=True,
        closing_speech=_held_closing_line(record, clip_ids, cuts[-1] if cuts else None),
        source_audio_kept=kept if isinstance(kept, bool) else None,
        source_clip_ids=tuple(prints),
        duplicate_clip_positions=_same_file_positions(clip_ids, prints) if prints else None,
        source_duplicate_copies=len(prints) - len(set(prints.values())) if prints else None,
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
        reason=_loc(reason)[:300] if reason else None,
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
                say(
                    en=(
                        f"{len(bad)} of {len(facts.per_clip_text)} labels still show more than "
                        "the hour. A re-render can't reformat them; ask me again to change "
                        "them in the editor."
                    ),
                    tr=(
                        f"{len(facts.per_clip_text)} etiketin {len(bad)} tanesi hâlâ saatten "
                        "fazlasını gösteriyor. Videoyu yeniden oluşturmak bunları düzeltmez; "
                        "editörde değiştirmem için tekrar iste."
                    ),
                ),
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
            return _receipt(
                req,
                "partial",
                say(
                    en="That exact text isn't in this edit.",
                    tr="Bu tam yazı bu düzenlemede yok.",
                ),
            )
        return _receipt(req, "partial", _CANT_CHECK_EDITOR_CLIP_TEXT)
    if facts.editor and wanted and _wants_exact_text(req):
        # "just say X" / "X only": every clip this turn touched must read exactly X.
        off = [c for c, t in facts.per_clip_text.items() if _fold(t) != wanted]
        if off:
            total = len(facts.per_clip_text)
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{len(off)} of {total} text{'s' if total != 1 else ''} "
                        f"didn't end up reading exactly \u201c{req.literal}\u201d."
                    ),
                    tr=(
                        f"{total} yazının {len(off)} tanesi tam olarak "
                        f"\u201c{req.literal}\u201d olmadı."
                    ),
                ),
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
                say(
                    en="That clip's label repeated the clip before it, so I left it off.",
                    tr=("O klibin etiketi önceki klibin etiketiyle aynıydı, o yüzden koymadım."),
                ),
            )
        if value is None:
            return _receipt(
                req,
                "not_possible",
                say(
                    en="That clip didn't get its own text in this draft.",
                    tr="O klibe bu taslakta kendi yazısı gelmedi.",
                ),
            )
        if wanted and not _contains_text(value, wanted):
            return _receipt(
                req,
                "partial",
                say(
                    en="That clip's text isn't the exact text you gave.",
                    tr="O klibin yazısı verdiğin yazıyla aynı değil.",
                ),
            )
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
                req,
                "partial",
                say(
                    en="The clips don't carry the exact text you gave.",
                    tr="Kliplerde verdiğin tam yazı yok.",
                ),
                inferred,
                guessed,
            )
    if total and count >= total:
        if facts.unreadable_label_clip_ids:
            n = len(facts.unreadable_label_clip_ids)
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{n} clip{'s are' if n != 1 else ' is'} too short for its text to stay "
                        "on screen long enough to read."
                    ),
                    tr=f"{n} klip, yazısının okunacak kadar ekranda kalması için çok kısa.",
                ),
                inferred,
                guessed,
            )
        return _receipt(req, "met", None, inferred, guessed)
    if count == 0:
        return _receipt(
            req,
            "not_possible",
            say(
                en=(
                    "No clip got its own text in this draft."
                    if not total
                    else f"None of the {total} clips got its own text in this draft."
                ),
                tr=(
                    "Bu taslakta hiçbir klibe kendi yazısı gelmedi."
                    if not total
                    else f"Bu taslakta {total} klibin hiçbirine kendi yazısı gelmedi."
                ),
            ),
        )
    reason = say(
        en=(f"Text landed on {count} of {total} clips." if total else f"Text on {count} clips."),
        tr=(
            f"Yazı {total} klibin {count} tanesine geldi." if total else f"{count} klipte yazı var."
        ),
    )
    repeats = len(facts.repeat_label_clip_ids)
    if repeats:
        # Left off on purpose: the same name twice in a row adds nothing to the viewer.
        reason = say(
            en=(
                f"{reason[:-1]}; {repeats} more repeated the label before, so I left "
                f"{'them' if repeats != 1 else 'it'} off."
            ),
            tr=(f"{reason[:-1]}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım."),
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
            say(
                en="The labels are place names, not the hour each clip was filmed.",
                tr="Etiketler yer adı, klibin çekildiği saat değil.",
            ),
        )
    if len(timed) < total:
        missing = total - len(timed)
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"Filming hour on {len(timed)} of {total} clips; "
                    f"{missing} {'have' if missing != 1 else 'has'} no filming time."
                ),
                tr=(
                    f"Çekim saati {total} klibin {len(timed)} tanesinde var; "
                    f"{missing} klibin çekim saati yok."
                ),
            ),
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
    return say(
        en=(
            f"Your clips were filmed starting at {saw_first} and ending at {saw_last}, the "
            f"reverse of the route you gave ({_short(start)} → {_short(end)}). I kept filming "
            "order; tell me if you want your route order instead."
        ),
        tr=(
            f"Klipler şöyle çekilmiş: başlangıç {saw_first}, bitiş {saw_last}. Bu, verdiğin "
            f"rotanın ({_short(start)} → {_short(end)}) tersi. Çekim sırasını korudum; rota "
            "sırasını istersen söyle."
        ),
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
    if facts.narrated_steps is not None:
        return _check_narrated_order(req, facts)
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
                say(
                    en=(
                        f"This draft is ordered by {basis.replace('_', ' ')}, not the order "
                        "you asked for."
                    ),
                    tr=(
                        "Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: "
                        f"{_BASIS_TR.get(basis, basis.replace('_', ' '))}."
                    ),
                ),
            )
    elif facts.sequence_statuses:
        # The creator described the order in their own words; the plan placed (or
        # failed to place) each group and recorded which (KRI-458).
        landed = sum(status == "met" for status in facts.sequence_statuses)
        if landed < len(facts.sequence_statuses):
            return _receipt(
                req,
                "partial" if landed or not required else "not_possible",
                say(
                    en="some of the groups you named are not where you said",
                    tr="söylediğin gruplardan bazıları dediğin yerde değil",
                ),
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
            said.append(
                say(
                    en=f"these aren't where you asked: {', '.join(facts.sequence_unmet)}",
                    tr=f"bunlar istediğin yerde değil: {', '.join(facts.sequence_unmet)}",
                )
            )
        if facts.sequence_absent:
            said.append(
                say(
                    en=f"I found no clips for: {', '.join(facts.sequence_absent)}",
                    tr=f"şunlar için klip bulamadım: {', '.join(facts.sequence_absent)}",
                )
            )
        return _receipt(
            req,
            unmet,
            say(
                en=f"Your clips are in filming order, but {'; '.join(said)}.",
                tr=f"Klipler çekim sırasında, ama {'; '.join(said)}.",
            ),
        )
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
            say(
                en=(
                    f"{n} clip{'s' if n != 1 else ''} had no capture time, so I kept "
                    f"{'their' if n != 1 else 'its'} attachment order."
                ),
                tr=(f"{n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım."),
            ),
        )
    if key in _CAPTURE_ORDER_KEYS and facts.ordering_choice == "group_first":
        # Honest: the creator picked grouping over strict filming order, so the order
        # holds inside each group only.
        return _receipt(
            req,
            "met",
            say(
                en="you chose grouping first, so it's in filming order inside each group",
                tr="önce gruplamayı seçtin, o yüzden her grubun içinde çekim sırasında",
            ),
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
# Who says the trigger: the creator in the first or second person ("when I say ..."), or,
# because the brief rewrites asks into third person, their voice ("when the voiceover
# says ...", "when the narration mentions ...").
_CUE_SPEAKER = (
    r"(?:(?:i|you|we)"
    r"|(?:my |the |our |this |her |his )?"
    r"(?:voice[- ]?over|narration|narrator|recording|audio|script|voice|speech|commentary))"
)
_CUE_VERB = (
    r"(?:say|says|mention|mentions|hear|hears|name|names|talk(?:s)? about|read(?:s)?"
    r"|bring(?:s)? up|get(?:s)? to)"
)
_CUE_OPENER = r"(?:when|whenever|as soon as|the moment|the second|every time)"
# The creator's voice is the trigger ("when I say ...", "dediğimde"). Without a named word,
# only this counts as a pop-in ask; "pop up" and "sticker" alone do not.
_SPOKEN_CUE_RE = re.compile(
    rf"\b{_CUE_OPENER} {_CUE_SPEAKER} {_CUE_VERB}\b|\bwhen you hear\b"
    r"|\bat (the |each |every )?(exact |spoken |specific )?words?\b|\bword[- ]triggered\b"
    r"|dedi[ğg]i[mn]de|deyince|denince|dendi[ğg]inde|s[öo]yledi[ğg]i[mn]de|bahsetti[ğg]i[mn]de"
    r"|ge[çc]ti[ğg]i[mn]de|ge[çc]ince"
    # KRI-540: the passive / spoken-word forms the Kadıköy brief used ("'İlk durak'
    # dendiğinde", "her 'kahve' kelimesinde"). Written against `_fold` output
    # (ı -> i), tolerant of ASCII-typed ğ/ç/ş/ö/ü. Turkish "every X" ("her 'X'de",
    # "her X'te") REQUIRES a quote or apostrophe suffix so English "her" never matches.
    r"|\b(?:dend[ıi][ğg]inde|denince|denild[ıi][ğg]inde|derken|ded[ıi][ğg]inde"
    r"|s[öo]ylend[ıi][ğg]inde|s[öo]yleyince|ge[çc]t[ıi][ğg]inde|ge[çc]ince|ge[çc]erken"
    r"|duyuld[uü][ğg]unda|duyunca)\b"
    r"|\bkelimesi(?:nde|ni)\b|\bs[öo]zc[üu][ğg][üu]nde\b|\bs[öo]z[üu]nde\b"
    r"|\bher\s+[\"'\u201c\u201d\u2018\u2019\u00ab\u00bb][^\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]{1,60}"
    r"[\"'\u201c\u201d\u2018\u2019\u00ab\u00bb](?:d[ea]|t[ea])\b"
    r"|\bher\s+[^\s\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]{1,40}['\u2019](?:d[ea]|t[ea])\b"
)
_BEAT_CUE_RE = re.compile(
    rf"{_SPOKEN_CUE_RE.pattern}|\bpop(s|ping)?[- ]?(up|in|ups|ins)\b|\b(sticker|stamp)s?\b"
    r"|\b[çc]ikartma"
)
# The words after the cue verb ("... when voiceover says the medal, show ..."): the trigger
# the creator named without quoting it.
_CUE_TRIGGER_RE = re.compile(
    rf"\b{_CUE_OPENER} {_CUE_SPEAKER} {_CUE_VERB}\s+"
    r"(?:the (?:words?|phrase|name) )?(?P<trigger>.+)",
    re.IGNORECASE,
)
# The trigger ends at punctuation or at ", and show ..." (a conjunction and an action verb).
_CUE_TRIGGER_END_RE = re.compile(
    r"[,;.!?:]|\s+(?:and|then|so|but)\s+(?:then\s+)?(?:show|put|pop|display|add|play|flash|insert"
    r"|overlay|throw|cut|bring)\b",
    re.IGNORECASE,
)
# A described kind of word ("any food name") is not a spoken phrase to look for.
_GENERIC_TRIGGER_RE = re.compile(
    r"\b(?:any|every|each|all|some|something|anything|whatever|whichever|names?|words?|things?"
    r"|numbers?|phrases?|places?|people|persons?|countries|country|cities|city|them|it|that"
    r"|those|these|her|hepsi\w*|herhangi)\b",
    re.IGNORECASE,
)
_TRIGGER_STOPWORDS = frozenset(
    "a an the it its we i you he she they me my our your his her their this that these those "
    "to of and or but so then is are was were be been in on at for with from by as if".split()
)
_CLOSING_RE = re.compile(
    r"\b(finish|end|close|wrap up|wrap) (on|with)\b|\bending (on|with|shot)\b"
    r"|\bclosing (shot|photo|image|picture|frame)\b|\blast (shot|frame)\b"
    r"|\bbitir|\bkapan[iı]ş"
)
_SOUND_RE = re.compile(
    r"\b(sound|sounds|sfx|buzzer|ding|beep|whoosh|swoosh|boing|horn|applause)\b|\bses(?:i|ini|iyle|ler|leri|lerini)?\b|\befekt"
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
    r"\bretakes?\b|\brestart(s|ed|ing)?\b|\bfalse starts?\b"
    r"|\b(the )?(part|bit|place|section|moment) where\b"
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
# The brief files "show the medal photo when the voiceover says the medal" as `timing`
# as often as `style`/`audio`/`select` (KRI-537); only the pop-in check reads that kind.
_BEAT_ASK_KINDS = _BEAT_KINDS | {"timing"}
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
    # Source-preservation is a global (or explicitly clip-scoped) timing ask.
    # Title-scoped timing requirements describe a text element's hold window;
    # treating their words (for example, "whole video") as source language
    # incorrectly routes them through the whole-take checker.
    if req.kind != "timing" or req.scope == "title" or _has_duration_target(req):
        return False
    return bool(req.facts.get("keep_whole_take") or _WHOLE_TAKE_RE.search(_req_text(req)))


_WHOLE_TEXT_RE = re.compile(
    r"\b(texts?|titles?|labels?|captions?)\b.{0,40}\b(whole|full|entire)\s+"
    r"(video|edit|take)\b|\b(whole|full|entire)\s+(video|edit|take)\b.{0,40}"
    r"\b(texts?|titles?|labels?|captions?)\b"
)


def _wants_whole_text_span(req: BriefRequirement) -> bool:
    """Whether a title timing ask requests persistence for the full output."""
    if req.kind != "timing" or req.scope != "title" or _has_duration_target(req):
        return False
    return bool(
        req.facts.get("persistent")
        or req.facts.get("keep_visible")
        or _WHOLE_TEXT_RE.search(_req_text(req))
    )


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


def _cue_triggers(req: BriefRequirement) -> list[str]:
    """The words right after a cue verb ("when voiceover says the medal, show ...").

    A guess at what the creator named without quoting it, in their own spelling. It only
    ever CONFIRMS (picks which pop-ins an ask is about, recognises an unheard word); a
    guess that matches nothing never becomes a "No pop-in for X" verdict.
    """
    for field_value in (req.description, req.literal):
        match = _CUE_TRIGGER_RE.search(field_value or "")
        if match is None:
            continue
        words = _CUE_TRIGGER_END_RE.split(match.group("trigger"), maxsplit=1)[0]
        words = " ".join(words.split()).strip(" \"'\u201c\u201d")
        tokens = [t for t in re.split(r"\W+", _fold(words)) if t]
        # A described kind of word ("any food name") names nothing to look for.
        if (
            2 < len(words) <= 80
            and len(tokens) <= 8
            and not all(t in _TRIGGER_STOPWORDS for t in tokens)
            and not _GENERIC_TRIGGER_RE.search(words)
        ):
            return [words]
    return []


def _dedupe_names(named: Iterable[str]) -> list[str]:
    out: dict[str, str] = {}
    for name in named:
        cleaned = " ".join(str(name).split())
        if cleaned and len(cleaned) <= 80:
            out.setdefault(_fold(cleaned), cleaned)
    return list(out.values())


def _quoted_triggers(req: BriefRequirement) -> list[str]:
    found: list[str] = []
    for field_value in (req.description, req.literal):
        found.extend(_QUOTED_RE.findall(field_value or ""))
    return found


def _named_triggers(req: BriefRequirement) -> list[str]:
    """The spoken words the creator named for certain: from `facts`, else quoted."""
    return _dedupe_names(_fact_triggers(req) or _quoted_triggers(req))


# Without a named word, a cue like "when I say ..." is a pop-in ask only when something is
# shown or played on it: a photo, sticker, sound, video card, the pop-in itself, or a
# show/put/play verb. "When I say go, cut to the next clip" is a cut (its own timing,
# unverified here), not a pop-in.
_TIMING_POP_IN_RE = re.compile(
    rf"{_VISUAL_RE.pattern}|{_SOUND_RE.pattern}"
    r"|\b(?:videos?|clips?|footage|cards?|overlays?|pop[- ]?(?:ups?|ins?)|on[- ]screen)\b"
    r"|\b(?:show|shows|put|puts|pop|pops|display|displays|flash|flashes|overlay|overlays"
    r"|add|adds|play|plays|bring|brings|insert|inserts|place|places)\b"
    r"|\bg[öo]rsel|\bvideo|\bkli[pb]|\bg[öo]ster|\b[çc][ıi]k(?:s[ıi]n|ar|)\b|\bekle|\bkoy"
    # KRI-540 Turkish sound beats: "zil çal" (ring a bell), "ses çalsın" (play a sound).
    r"|\bzil\b|\b[çc]al(?:s[ıi]n)?\b|\bduyulsun\b"
)
# Cut and edit verbs: a sentence that has one is an edit instruction, not a pop-in.
_TIMING_CUT_RE = re.compile(
    r"\b(?:cut|jump|switch|transition|move|go|skip|end|stop|start|begin|trim|speed|slow|fade"
    r"|pause|freeze|zoom|rewind|remove|delete)(?:s|es|ing)?\b"
    r"|\b(?:ge[çc]|atla|kes|dur|bitir|ba[şs]la|yava[şs]la|h[ıi]zlan)(?:sin|sun|s[üu]n)?\b"
)
# An explicit "show a picture / pop up / sticker" marker: the ask is about visuals even if
# it also has a cut verb ("pop up a sticker when I say pause, cut the rest").
_EXPLICIT_POP_RE = re.compile(
    rf"\bpop(?:s|ping)?[- ]?(?:up|in|ups|ins)\b|{_VISUAL_RE.pattern}"
    r"|\bfotograf|\bgorsel|\b[çc]ikartma"
)


def _cleanup_wording(req: BriefRequirement) -> bool:
    """Removal verb + something speech cleanup removes ("cut the pauses", "remove the ums")."""
    text = _req_text(req)
    if _KEEP_PAUSES_RE.search(text) or not _CLEANUP_VERB_RE.search(text):
        return False
    return bool(_CLEANUP_NOUN_RE.search(text) or _NAMED_CUT_RE.search(text))


def _wants_beats(req: BriefRequirement) -> bool:
    if req.kind not in _BEAT_ASK_KINDS:
        return False
    if req.kind == "timing" and (
        # Timing asks about length or a text's hold window keep their own checkers.
        req.scope == "title"
        or _has_duration_target(req)
        or _wants_whole_take(req)
        or _wants_whole_text_span(req)
    ):
        return False
    text = _req_text(req)
    if _fact_triggers(req):
        return True
    spoken = _SPOKEN_CUE_RE.search(text)
    if _quoted_triggers(req):
        # A named word with a cue is a pop-in ask (a timing ask needs the spoken cue).
        return bool(spoken if req.kind == "timing" else _BEAT_CUE_RE.search(text))
    if not spoken:
        # The original rule for a style/audio/select ask: an explicit pop-in or sticker word
        # is enough ("add stickers for each food", "pop up the pasta sticker"). A timing
        # ask needs the spoken cue, so "make the text pop up quickly" stays a timing ask.
        return req.kind != "timing" and bool(_BEAT_CUE_RE.search(text))
    # No named word: a spoken cue alone also starts cuts, speed changes and cleanup
    # ("every time I say um, cut it out"). Only a visual/sound pop-in object counts.
    if _EXPLICIT_POP_RE.search(text):
        return True
    return bool(
        _TIMING_POP_IN_RE.search(text)
        and not _TIMING_CUT_RE.search(text)
        and not _cleanup_wording(req)
    )


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
    return _cleanup_wording(req)


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


# "Keep the crowd noise quiet under my voiceover": how loud the footage's own sound plays
# beneath the creator's voice (KRI-537). Patterns run on `_fold` output.
_SOUND_NOUN = r"(?:noise|noises|sound|sounds|audio|chatter|ambience|volume)"
# Names the footage's own sound. Music is never footage sound here (a licensed track or a
# "background music" ask is judged elsewhere).
_FOOTAGE_SOUND_STRICT = (
    rf"(?:(?:crowd|ambient|street|room|wind|traffic|natural|original|raw|footage|clip|clips"
    rf"|video|camera) {_SOUND_NOUN}"
    r"|(?:sound|audio|noise)s? (?:of|from|in) the (?:footage|clips?|video|crowd)"
    r"|kalabalik\w*|orijinal ses\w*|ortam ses\w*|[çc]ekim\w* ses\w*"
    r"|klip\w* ses\w*|video\w* ses\w*)"
)
# "background noise" counts for a keep-it-quiet ask, not for a mute (that may mean
# noise reduction on the voice recording).
_FOOTAGE_SOUND_LOOSE = (
    rf"(?:{_FOOTAGE_SOUND_STRICT}|(?:background|bg) {_SOUND_NOUN}"
    r"|arka ?plan\w* (?:ses|g[üu]r[üu]lt[üu])\w*)"
)
_BED_NOISE_RE = re.compile(rf"\b{_FOOTAGE_SOUND_LOOSE}\b|\bg[üu]r[üu]lt[üu]")
_BED_NAMED_RE = re.compile(rf"\b{_FOOTAGE_SOUND_STRICT}\b")
# Quiet wording about the bed. Bare adjectives ("soft", "calm") only count with
# under/beneath, which are in the list themselves.
_BED_QUIET_RE = re.compile(
    r"\b(?:quiet(?:er)?|low(?:er)?|down|under|beneath|below|underneath|duck(?:ed|ing)?"
    r"|in the background|turned down)\b"
    r"|\bkisik|\bkis(?:il|ili|in)|\bd[üu][şs][üu]k|\balt[iı]nda|\barka planda|\bsessiz"
)
# The opposite direction ("raise the crowd noise", "not too quiet").
_BED_UP_RE = re.compile(
    r"\b(?:raise|louder|up|increase|boost|amplify|higher|too quiet|so quiet)\b"
    r"|\byukselt|\byükselt|\bart[iı]r|\bdaha y[üu]ksek"
)
_BED_MUTE_RE = re.compile(
    r"\b(?:mute|muted|silence|silenced|kill|strip|remove|delete|drop|get rid of|cut out|turn off"
    r"|disable|no|without)\b|\bkapat|\bkaldir|\bsil\b|\bolmasin|\bolmadan"
)


def _wants_bed_under_voice(req: BriefRequirement) -> bool:
    """Keep the footage's own sound (crowd, ambience, original audio) low under the voice."""
    if req.kind not in _BEAT_KINDS or _wants_beats(req) or _wants_cleanup(req):
        return False
    text = _req_text(req)
    return bool(
        _BED_NOISE_RE.search(text) and _BED_QUIET_RE.search(text) and not _BED_UP_RE.search(text)
    )


def _wants_bed_muted(req: BriefRequirement) -> bool:
    """Mute or remove the footage's own sound, with no "keep it low" wording."""
    if req.kind not in _BEAT_KINDS or _wants_beats(req) or _wants_cleanup(req):
        return False
    text = _req_text(req)
    return bool(
        _BED_NAMED_RE.search(text) and _BED_MUTE_RE.search(text) and not _BED_QUIET_RE.search(text)
    )


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
    if extra <= 0:
        return shown
    return say(en=f"{shown} and {extra} more", tr=f"{shown} ve {extra} tane daha")


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
_CANT_CHECK_STYLE = "I can't check this text style automatically."
_CANT_CHECK_MIX = "I can't check the sound mix on this draft yet."
# Start of the reason on a "mute the footage sound" ask the voiceover mix cannot fully meet
# (the footage sound is a quiet bed under the voice, never off): a limit of the format.
_BED_STILL_PLAYS = "The footage sound still plays"
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
# KRI-541: what speech cleanup did on a finished render.
_CLEANUP_DONE = "Speech cleanup cut the long pauses"
_CLEANUP_NOTHING_TO_CUT = "Speech cleanup found no long pauses to cut"
_CLEANUP_NOT_RUN = "Speech cleanup didn't run on this video, so the long pauses stay"

# The Turkish twin of every static reason above (KRI-520). Reasons are written into
# receipts when a turn runs, in that turn's language, so everything that tells one
# reason from another (neutral, format limit, wordless title) accepts both texts;
# `_loc` also turns a reason a worker stored in English into Turkish for a Turkish reply.
_REASON_TR: dict[str, str] = {
    _CANT_CHECK_EDITOR_CLIP_TEXT: "Editör düzenlemesinde klip başına yazıyı doğrulayamıyorum.",
    _CANT_CHECK_TARGET_CLIP_TEXT: "Bu taslakta o klibin yazısını doğrulayamıyorum.",
    _CANT_CONFIRM_ORDER: "Bu taslağın kullandığı sırayı doğrulayamıyorum.",
    _CANT_CHECK_ORDER_RULE: "Bu sıralamayı otomatik olarak doğrulayamıyorum.",
    _ORDER_NOT_APPLIED: (
        "Anlattığını kliplerle eşleştiremedim, o yüzden klipler eklediğin sırada kaldı."
    ),
    _ORDER_NOT_RECORDED: "Düzenlemenin kullandığı sırayı doğrulayamadım.",
    _ORDER_RULE_NOT_APPLIED: (
        "Bu sıralama kuralını doğrulayamıyorum ve klipler kurala uyduğunu gösterebileceğim "
        "bir sırada değil."
    ),
    _CANT_CHECK_BEATS: "Bu taslakta sözlerine göre çıkan görselleri henüz kontrol edemiyorum.",
    _CANT_CHECK_SPEECH: "Bu taslaktaki konuşma bölümlerini henüz kontrol edemiyorum.",
    _CANT_CHECK_TAKE: "Bu taslağın çekimini baştan sona koruduğunu doğrulayamıyorum.",
    _CANT_CHECK_TITLE: "Bu taslaktaki başlığın nereden geldiğini doğrulayamıyorum.",
    _NO_TITLE: (
        "Başlık eklemedim, çünkü kullanabileceğim bir yazın ya da isteğinde dayanabileceğim "
        "bir bilgi yoktu."
    ),
    _CANT_CONFIRM_LENGTH: "Bu taslağın uzunluğunu henüz doğrulayamıyorum.",
    _TALKING_KEEPS_WHOLE_TAKE: (
        "Konuşmalı düzenleme çekimini baştan sona korur, o yüzden uzunluğu klibini izler"
    ),
    _VOICEOVER_SETS_LENGTH: "Seslendirmeli düzenleme seslendirmen kadar sürer",
    _CANT_CHECK_TIMING: "Bu süreyi otomatik olarak doğrulayamıyorum.",
    _CANT_CHECK_CLEANUP: "Bu taslakta konuşma temizliğini henüz kontrol edemiyorum.",
    _CANT_CHECK_CAPTIONS: "Bu taslaktaki altyazıları henüz kontrol edemiyorum.",
    _NO_CHECKER: "Bunu henüz otomatik olarak doğrulayamıyorum.",
    _CANT_CHECK_STYLE: "Bu yazı stilini otomatik olarak kontrol edemiyorum.",
    _CANT_CHECK_MIX: "Bu taslakta ses karışımını henüz kontrol edemiyorum.",
    _BED_STILL_PLAYS: "Çekim sesi hâlâ çalıyor",
    _CLEANUP_PLANNED: "Konuşma temizliği uzun duraklamaları keser",
    _CLEANUP_AT_APPROVAL: (
        "Onaylarken \u201cKonuşmayı temizle\u201d seçeneğini seç, uzun duraklamalar kesilir"
    ),
    _CLEANUP_IF_OFFERED: (
        "Onaylarken \u201cKonuşmayı temizle\u201d çıkarsa, uzun duraklamaları kesmek için onu seç"
    ),
    _CLEANUP_UNAVAILABLE: (
        "Konuşma temizliği bu proje için henüz yok, o yüzden duraklamalar kalıyor"
    ),
    _NAMED_CUTS_NEED_EDITOR: (
        "tekrar çekim ya da belirli bir cümle henüz otomatik kesilmiyor, onu editörde kırp"
    ),
    _CAPTIONS_WORD_BY_WORD: "kelimeler sen söylerken yanıyor",
    _CLEANUP_DONE: "Konuşma temizliği uzun duraklamaları kesti",
    _CLEANUP_NOTHING_TO_CUT: "Konuşma temizliği kesilecek uzun bir duraklama bulmadı",
    _CLEANUP_NOT_RUN: ("Konuşma temizliği bu videoda çalışmadı, o yüzden uzun duraklamalar kaldı"),
}


def _loc(reason: str) -> str:
    """A static English reason in the chat's language; any other text comes back as is."""
    return say(en=reason, tr=_REASON_TR.get(reason, reason))


# NO_TITLE_REASON is the English text; a receipt written in a Turkish turn carries the
# Turkish twin, so compare with `is_no_title_reason`.
_NO_TITLE_REASONS = frozenset({_NO_TITLE, _REASON_TR[_NO_TITLE]})


def is_no_title_reason(reason: object) -> bool:
    """True for the receipt reason of a title requirement that came with no words."""
    return isinstance(reason, str) and reason in _NO_TITLE_REASONS


_NEUTRAL_EN = frozenset(
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
        _CANT_CHECK_MIX,
        _CANT_CHECK_STYLE,
        _NO_CHECKER,
    }
)
_NEUTRAL_REASONS = _NEUTRAL_EN | frozenset(_REASON_TR[reason] for reason in _NEUTRAL_EN)

# Reasons that describe what the format the creator chose does with an ask, not a
# simplification Kria made instead of it: a Talking edit's length follows the take,
# speech cleanup is chosen at approval and cuts pauses, never a named line. The
# receipt stays an honest "Partly"; it never turns a first draft into the "should I
# make a simpler version?" question (`needs_creator_choice`), which only makes sense
# when there is a different, simpler plan to choose.
_FORMAT_LIMIT_EN: tuple[str, ...] = (
    _TALKING_KEEPS_WHOLE_TAKE,
    _VOICEOVER_SETS_LENGTH,
    _CLEANUP_PLANNED,
    _CLEANUP_AT_APPROVAL,
    _CLEANUP_IF_OFFERED,
    _CLEANUP_UNAVAILABLE,
    _CLEANUP_DONE,
    _CLEANUP_NOTHING_TO_CUT,
    _CLEANUP_NOT_RUN,
    _BED_STILL_PLAYS,
)
_FORMAT_LIMIT_REASON_PREFIXES: tuple[str, ...] = (
    *_FORMAT_LIMIT_EN,
    *(_REASON_TR[reason] for reason in _FORMAT_LIMIT_EN),
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
            say(
                en=(
                    f"This {facts.edit_format.replace('_', ' ')} edit cuts your footage down; "
                    "a Talking edit keeps the whole take."
                ),
                tr=(
                    f"Bu düzenleme ({_FORMAT_TR.get(facts.edit_format, facts.edit_format)}) "
                    "çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur."
                ),
            ),
        )
    if facts.video_clip_count is None:
        return _receipt(req, "partial", _CANT_CHECK_TAKE)
    if facts.video_clip_count != 1:
        return _receipt(
            req,
            "partial",
            say(
                en="This edit uses one of your clips, not every take.",
                tr="Bu düzenleme kliplerinden yalnızca birini kullanıyor, her çekimi değil.",
            ),
        )
    if facts.speech_cleanup_enabled:
        return _receipt(
            req,
            "partial",
            say(
                en="Speech cleanup is on, so some pauses or retakes may be cut.",
                tr=(
                    "Konuşma temizliği açık, o yüzden bazı duraklamalar ya da tekrar "
                    "çekimler kesilebilir."
                ),
            ),
        )
    return _receipt(req, "met", None)


def _unheard_for(facts: PlanFacts, names: Sequence[str]) -> list[str]:
    """The never-heard triggers this ask is about: only the ones it names.

    An ask that names nothing is not blamed for another beat's unheard word.
    """
    return [t for t in facts.unheard_beat_triggers if any(_trigger_heard(n, t) for n in names)]


def _never_heard_problem(triggers: Iterable[str]) -> str:
    names = _names(triggers)
    return say(
        en=f"I never heard {names} in your voice",
        tr=f"Sesinde {names} sözünü duymadım",
    )


def _placement_reason(placements: Sequence[BeatFact]) -> str | None:
    """Where the met pop-ins landed, in time order; None when the render gave no times."""
    timed = sorted((b for b in placements if b.at_s is not None), key=lambda b: b.at_s or 0.0)
    if not timed:
        return None
    shown = timed[:_MAX_NAMED_IN_REASON]
    more = len(timed) - len(shown)
    items_en = [f'"{b.trigger}" at {b.at_s:.1f} s' for b in shown]
    if more > 0:
        items_en.append(f"{more} more")
    joined_en = (
        items_en[0] if len(items_en) == 1 else ", ".join(items_en[:-1]) + " and " + items_en[-1]
    )
    parts_tr = [f'"{b.trigger}" {b.at_s:.1f} sn\'de' for b in shown]
    tail_tr = f" ve {more} tane daha" if more > 0 else ""
    return say(
        en=f"{'Pop-in' if len(timed) == 1 else 'Pop-ins'} on {joined_en}",
        tr=f"{', '.join(parts_tr)}{tail_tr} çıkıyor",
    )


def _check_reaction_beats(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Word-triggered pop-ins and the closing shot, from the repaired strategy."""
    wants_beats = _wants_beats(req)
    wants_closing = _wants_closing(req)
    if facts.reaction_beats is None:
        return _receipt(req, "partial", _CANT_CHECK_BEATS)
    beats = facts.reaction_beats
    problems: list[str] = []
    delivered = False
    placements: list[BeatFact] = []
    by_name = True
    if wants_beats:
        # Words the creator named for certain (facts, quotes) can be "missing"; words only
        # guessed from the sentence pick which pop-ins the ask is about, never a verdict.
        named = _named_triggers(req)
        names = named or _cue_triggers(req)
        if not facts.reaction_beats_available:
            problems.append(
                say(
                    en="Photo and sound pop-ins timed to your words aren't available for this "
                    "edit yet",
                    tr="Sözlerine göre çıkan fotoğraf ve ses efektleri bu düzenleme için henüz yok",
                )
            )
        elif not beats:
            # Nothing was placed at all: every unheard word is this ask's problem.
            unheard = list(facts.unheard_beat_triggers) if not names else _unheard_for(facts, names)
            if unheard:
                problems.append(_never_heard_problem(unheard))
            else:
                problems.append(
                    say(
                        en=(
                            "I couldn't find the photos or stickers for "
                            f"{_names(facts.dropped_beat_triggers)}"
                        ),
                        tr=(
                            "Şunlar için fotoğraf ya da çıkartma bulamadım: "
                            f"{_names(facts.dropped_beat_triggers)}"
                        ),
                    )
                    if facts.dropped_beat_triggers
                    else say(
                        en="This draft has no pop-ins timed to your words",
                        tr="Bu taslakta sözlerine göre çıkan görsel yok",
                    )
                )
        else:
            unheard = _unheard_for(facts, names)
            matched = [b for b in beats if any(_trigger_heard(n, b.trigger) for n in names)]
            by_name = True
            if not names:
                placements = list(beats)
            elif matched:
                placements = matched
            elif all(any(_trigger_heard(n, u) for u in unheard) for n in names):
                # Everything this ask is about was never said: nothing of it landed.
                placements = []
            else:
                # The sentence's own words matched no beat: judge the pop-ins as a whole
                # (and quote no times, which would belong to other words).
                placements = list(beats)
                by_name = False
            delivered = bool(placements)
            # A word the render never heard is told as that, not as a missing pop-in.
            missing = [
                n
                for n in named
                if not any(_trigger_heard(n, b.trigger) for b in beats)
                and not any(_trigger_heard(n, u) for u in unheard)
            ]
            if unheard:
                problems.append(_never_heard_problem(unheard))
            unresolved = [
                t
                for t in facts.dropped_beat_triggers
                if not any(_trigger_heard(t, b.trigger) for b in beats)
                and not any(_trigger_heard(t, n) for n in missing)
            ]
            if missing:
                problems.append(
                    say(
                        en=f"No pop-in for {_names(missing)}",
                        tr=f"Şunlar için çıkan görsel yok: {_names(missing)}",
                    )
                )
            if unresolved:
                problems.append(
                    say(
                        en=f"I couldn't find the photo or sticker for {_names(unresolved)}",
                        tr=(f"Şunlar için fotoğraf ya da çıkartma bulamadım: {_names(unresolved)}"),
                    )
                )
            text = _req_text(req)
            if placements:
                if _SOUND_RE.search(text) and not any(b.sound for b in placements):
                    problems.append(
                        say(
                            en="None of the pop-ins plays a sound",
                            tr="Çıkan görsellerin hiçbiri ses çalmıyor",
                        )
                    )
                if _VISUAL_RE.search(text) and not any(b.visual_id for b in placements):
                    problems.append(
                        say(
                            en="None of the pop-ins shows a photo or sticker",
                            tr="Çıkan görsellerin hiçbiri fotoğraf ya da çıkartma göstermiyor",
                        )
                    )
    if wants_closing:
        if facts.closing_visual_id is None:
            problems.append(
                say(
                    en="I couldn't find the closing photo you named",
                    tr="Söylediğin kapanış fotoğrafını bulamadım",
                )
                if facts.closing_requested
                else say(
                    en="This draft doesn't end on the photo you asked for",
                    tr="Bu taslak istediğin fotoğrafla bitmiyor",
                )
            )
        else:
            delivered = True
            if facts.closing_badge_requested and facts.closing_badge_id is None:
                problems.append(
                    say(
                        en="I couldn't find the closing badge you named",
                        tr="Söylediğin kapanış rozetini bulamadım",
                    )
                )
    if not problems:
        return _receipt(req, "met", _placement_reason(placements) if by_name else None)
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
            say(
                en="None of your lines made it in",
                tr="Cümlelerinden hiçbiri videoya girmedi",
            )
            + (f" ({missing})" if missing else ""),
        )
    if missing:
        problems.append(
            say(
                en=f"I couldn't find {missing} in your clip",
                tr=f"Klibinde şunu bulamadım: {missing}",
            )
        )
    if _SPEECH_OVER_RE.search(text) and not any(s.visual == "cutaways" for s in speech):
        problems.append(
            say(
                en="Your words never play over the other footage",
                tr="Sözlerin diğer çekimlerin üzerinde hiç çalmıyor",
            )
        )
    if _SPEAKER_RE.search(text) and not any(s.visual == "speaker" for s in speech):
        problems.append(
            say(
                en="It never cuts to you talking",
                tr="Hiçbir yerde konuşurken sana geçmiyor",
            )
        )
    # "Return to the speaker": a speaker shot that comes AFTER something that is not the speaker.
    first_other = next(
        (i for i, s in enumerate(sections) if s.kind == "montage" or s.visual == "cutaways"), None
    )
    if (
        first_other is not None
        and _SPEAKER_RE.search(text)
        and not any(s.visual == "speaker" for s in sections[first_other + 1 :])
    ):
        problems.append(
            say(
                en="It doesn't come back to you after the other footage",
                tr="Diğer çekimlerden sonra sana geri dönmüyor",
            )
        )
    if _BACK_TO_MONTAGE_RE.search(text) and not any(s.kind == "montage" for s in sections):
        problems.append(
            say(
                en="There are no fast cuts between your lines",
                tr="Cümlelerin arasında hızlı kesimler yok",
            )
        )
    if not problems:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "; ".join(problems) + ".")


def _check_speech_cleanup(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Pauses, retakes and filler the creator asked to cut, against the draft.

    Speech cleanup (the preflight check the creator confirms at approval) cuts long
    pauses and non-word sounds out of a Talking or voiceover edit; it never cuts a
    spoken line, so a retake or "the bit where I say X" is reported as editor work.
    A length the same sentence names ("keep it under 45 s") follows the cut take.
    Every honest outcome here is a limit of the format, not a simplification.
    """
    fmt = facts.edit_format
    if fmt is None or (facts.editor and not facts.rendered_variant) or fmt not in _SPEECH_FORMATS:
        # Only a speech-spined edit is judged here; a montage's cut is its own
        # planner's to report, so the ask stays "can't verify" there, as before.
        return _receipt(req, "partial", _CANT_CHECK_CLEANUP)
    if facts.rendered_variant:
        return _check_rendered_cleanup(req, facts)
    if facts.speech_cleanup_enabled:
        lead, status = _CLEANUP_PLANNED, "met"
    elif facts.speech_cleanup_offered is True:
        lead, status = _CLEANUP_AT_APPROVAL, "partial"
    elif facts.speech_cleanup_offered is False:
        lead, status = _CLEANUP_UNAVAILABLE, "partial"
    else:
        # Unknown whether approval offers the choice: never promise it.
        lead, status = _CLEANUP_IF_OFFERED, "partial"
    notes = [_loc(lead)]
    if _wants_named_cuts(req):
        notes.append(_loc(_NAMED_CUTS_NEED_EDITOR))
        status = "partial"
    if _has_duration_target(req):
        target = float(req.facts["duration_s"])
        notes.append(
            say(
                en=f"the length follows what's left of your take, so I can't promise {target:g}s",
                tr=(
                    "uzunluk çekimin geriye kalan kısmına göre belirleniyor, "
                    f"{target:g} sn için söz veremem"
                ),
            )
        )
        status = "partial"
    return _receipt(req, status, "; ".join(notes) if status == "partial" else None)


def _check_rendered_cleanup(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """KRI-541: the cleanup ask against what a finished render's speech cleanup did."""
    outcome = facts.speech_cleanup_outcome
    if outcome == "applied":
        lead, status = _CLEANUP_DONE, "met"
    elif outcome == "no_change":
        lead, status = _CLEANUP_NOTHING_TO_CUT, "met"
    elif outcome == "not_run":
        lead, status = _CLEANUP_NOT_RUN, "partial"
    else:
        return _receipt(req, "partial", _CANT_CHECK_CLEANUP)
    notes = [_loc(lead)]
    if _wants_named_cuts(req):
        # The cut removes pauses and filler sounds; a named line or retake stays.
        notes.append(_loc(_NAMED_CUTS_NEED_EDITOR))
        status = "partial"
    if _has_duration_target(req):
        target = float(req.facts["duration_s"])
        notes.append(
            say(
                en=f"the length follows what's left of your take, so it isn't held to {target:g}s",
                tr=(
                    "uzunluk çekimin geriye kalan kısmına göre belirlendi, "
                    f"{target:g} sn'ye göre ayarlanmadı"
                ),
            )
        )
        status = "partial"
    if status == "met" and outcome == "applied":
        return _receipt(req, "met", None)
    return _receipt(req, status, "; ".join(notes))


def _check_captions(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Captions on / off / word-by-word, against the strategy's caption style.

    Only what the style settles is judged: an ask about a caption's look, place or
    language stays "can't check", and so does ``"auto"`` (the item's own style,
    unknown here) for an on/off or word-by-word ask.
    """
    if facts.caption_language and not facts.editor:
        asked = _requested_caption_language(req)
        if asked is not None:
            return _check_caption_language(req, facts, asked)
    fmt = facts.edit_format
    text = _req_text(req)
    rendered = facts.rendered_variant
    if (
        facts.caption_style is None
        or (facts.editor and not rendered)
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
        return _receipt(
            req,
            "partial",
            say(
                en="Captions are on in this video."
                if rendered
                else "Captions are still on in this draft.",
                tr="Bu videoda altyazılar açık."
                if rendered
                else "Bu taslakta altyazılar hâlâ açık.",
            ),
        )
    if style == "none":
        return _receipt(
            req,
            "partial",
            say(
                en="This video has no captions." if rendered else "Captions are off in this draft.",
                tr="Bu videoda altyazı yok." if rendered else "Bu taslakta altyazılar kapalı.",
            ),
        )
    if _wants_word_captions(req):
        if style in {"karaoke", "kinetic"}:
            return _receipt(req, "met", _CAPTIONS_WORD_BY_WORD)
        if style == "auto":
            return _receipt(req, "partial", _CANT_CHECK_CAPTIONS)
        return _receipt(
            req,
            "partial",
            say(
                en="Captions are on as full sentences, not word by word.",
                tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
            ),
        )
    return _receipt(req, "met", None)


# A footage bed under this fraction of full volume counts as "quiet under the voice".
_BED_QUIET_BELOW = 0.5


def _check_voice_bed(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Keep the crowd noise quiet under my voice" / "mute the crowd noise".

    A voiceover edit always mixes the footage's own sound as a bed under the voice (a
    fraction of full volume), so a "keep it low" ask is met by the mix itself and a "mute
    it" ask can only be partly met: the bed stays, quietly. That limit belongs to the
    format, so it never turns the draft into the simpler-version question. Any other
    audio strategy is judged by its own receipts and stays unchecked here.
    """
    level = facts.voiceover_bed_level
    voiceover = facts.audio_strategy == "voiceover"
    if level is None and not voiceover:
        return _receipt(req, "partial", _CANT_CHECK_MIX)
    pct = None if level is None else round(level * 100)
    if _wants_bed_muted(req) and not _wants_bed_under_voice(req):
        if level is not None and level <= 0:
            return _receipt(req, "met", None)
        if level is not None and level >= 1.0:
            return _receipt(
                req,
                "partial",
                say(
                    en=(
                        f"{_BED_STILL_PLAYS} at full volume alongside your voice; "
                        "lower it in the editor's mix"
                    ),
                    tr=(
                        f"{_REASON_TR[_BED_STILL_PLAYS]}, sesinle aynı seviyede; "
                        "ayarı editördeki karışımdan düşür"
                    ),
                ),
            )
        about_en = "" if pct is None else f" (about {pct}%)"
        about_tr = "" if pct is None else f" (yaklaşık %{pct})"
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"{_BED_STILL_PLAYS} softly under your voice{about_en}; "
                    "lower it in the editor's mix"
                ),
                tr=(
                    f"{_REASON_TR[_BED_STILL_PLAYS]}, sesinin altında kısık{about_tr}; "
                    "ayarı editördeki karışımdan düşür"
                ),
            ),
        )
    if level is None:
        return _receipt(
            req,
            "met",
            say(
                en="Your footage sound plays under your voice",
                tr="Çekim sesi sesinin altında çalıyor",
            ),
        )
    if level >= 1.0:
        return _receipt(
            req,
            "partial",
            say(
                en="The footage sound plays at full volume alongside your voice",
                tr="Çekim sesi sesinle aynı seviyede, tam sesle çalıyor",
            ),
        )
    if level >= _BED_QUIET_BELOW:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"The footage sound plays at about {pct}% alongside your voice, "
                    "not far under it"
                ),
                tr=(
                    f"Çekim sesi sesinin yanında yaklaşık %{pct} seviyesinde çalıyor, "
                    "pek altında değil"
                ),
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en=f"Your footage sound sits under your voice at about {pct}% volume",
            tr=f"Çekim sesi, sesinin altında yaklaşık %{pct} seviyesinde",
        ),
    )


_STYLE_FIELD_NAMES = {
    "entrance": ("entrance animation", "giriş animasyonu"),
    "alignment": ("alignment", "hizalama"),
    "text_case": ("letter case", "harf biçimi"),
    "font_family": ("font", "yazı tipi"),
    "color": ("color", "renk"),
}


def _style_intent(req: BriefRequirement) -> dict[str, Any] | None:
    """The well-formed structured style intent of a style requirement, else None (KRI-543)."""
    if req.kind != "style":
        return None
    return normalize_style_intent(req.facts.get("style_intent"))


_TARGET_KIND = {"title": "title", "labels": "label"}


def _row_value(row: TextStyleRow, field_name: str) -> str | None:
    return getattr(row, field_name, None)


def _check_style(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    # A changed text lane proves a mutation, not that the requested fields, targets, or
    # animation relationships were satisfied (KRI-524). A value is compared only when the
    # ask resolves to a typed value (`derive_style_ask`: the structured intent, the facts the
    # extractor filled, or the creator's own unambiguous words), and only against rows whose
    # saved value is known.
    ask = derive_style_ask(req)
    if ask is None or facts.text_styles is None:
        return _receipt(req, "partial", _NO_CHECKER)
    rows = [
        row
        for row in facts.text_styles
        if ask.target in (None, "all_text") or row.kind == _TARGET_KIND[ask.target]
    ]
    if ask.target is None and facts.anaphora_rows is not None:
        # "…to all of them" on an editor turn means the texts this turn touched.
        touched = set(facts.anaphora_rows)
        rows = [row for row in rows if row.id in touched]
    verdicts = [
        value_matches(name, value, _row_value(row, name))
        for row in rows
        for name, value in ask.wanted.items()
    ]
    if not rows or None in verdicts:
        return _receipt(req, "partial", _CANT_CHECK_STYLE)
    off = [
        row
        for row in rows
        if any(value_matches(n, v, _row_value(row, n)) is False for n, v in ask.wanted.items())
    ]
    if not off:
        return _receipt(req, "met", None)
    if ask.target is None:
        # "…to all of them" may mean a subset: a mismatch is not evidence of a miss.
        return _receipt(req, "partial", _CANT_CHECK_STYLE)
    names_en = ", ".join(_STYLE_FIELD_NAMES[f][0] for f in ask.wanted)
    names_tr = ", ".join(_STYLE_FIELD_NAMES[f][1] for f in ask.wanted)
    return _receipt(
        req,
        "partial",
        say(
            en=f"{len(off)} of {len(rows)} texts don't have the requested {names_en} yet",
            tr=f"{len(rows)} metinden {len(off)} tanesinde istenen {names_tr} henüz yok",
        ),
    )


def _row_at(row: TextStyleRow, x: float, y: float) -> bool | None:
    """Whether a label row sits at an anchor; None when the row does not say where it is."""
    if row.position == "custom" and row.x_frac is not None and row.y_frac is not None:
        return abs(row.x_frac - x) <= 0.06 and abs(row.y_frac - y) <= 0.08
    named = {0.12: "top", 0.5: "middle", 0.78: "bottom"}.get(y)
    if row.position in ("top", "middle", "bottom") and named is not None:
        return row.position == named and abs(x - 0.5) <= 0.06
    return None


def _look_rows(req: BriefRequirement, facts: PlanFacts) -> list[TextStyleRow]:
    rows = list(facts.text_styles or ())
    if req.scope == "title":
        return [row for row in rows if row.kind == "title"]
    if req.scope == "per_clip":
        return [row for row in rows if row.kind == "label"]
    if req.scope.startswith("clip:"):
        target = _scope_clip_id(req, [row.clip_id for row in rows if row.clip_id])
        return [row for row in rows if row.kind == "label" and target and row.clip_id == target]
    if req.literal:
        wanted = _fold(req.literal)
        return [row for row in rows if _contains_text(row.text, wanted)]
    return rows


def check_text_look(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """Judge a TEXT requirement's look facts (title animation, label corner, font, colour).

    None when there is nothing to compare or a row does not say, so a record or draft that
    has no text lane never judges (and never blocks a render) on a look it cannot see.
    """
    look = text_look_facts(req)
    if not look or facts.text_styles is None:
        return None
    rows = _look_rows(req, facts)
    if not rows:
        return None
    misses: list[str] = []
    judged = 0
    undecided = False  # a fact we cannot read never hides a miss we already found
    for key, value in look.items():
        if key == "animation":
            wanted = normalize_title_animation(value)
            haves = [row.entrance for row in rows]
            label = ("animation", "animasyon")
            fails = [h != wanted for h in haves] if wanted else []
        elif key == "position":
            spot = normalize_label_position(value)
            if spot is None:
                continue
            x, y, _align = LABEL_ANCHORS[spot]
            haves = [_row_at(row, x, y) for row in rows]
            label = ("position", "konum")
            fails = [h is False for h in haves]
        elif key in ("font_family", "text_color"):
            ask = derive_style_ask(
                BriefRequirement(
                    id=req.id, kind="style", scope="global", description="", facts={key: value}
                )
            )
            if ask is None:
                continue
            name, wanted_value = next(iter(ask.wanted.items()))
            verdicts = [value_matches(name, wanted_value, _row_value(row, name)) for row in rows]
            haves = verdicts
            label = ("font", "yazı tipi") if key == "font_family" else ("colour", "renk")
            fails = [v is False for v in verdicts]
        else:
            continue
        if not fails or any(h is None for h in haves):
            undecided = True
            continue
        judged += 1
        if any(fails):
            misses.append(
                say(
                    en=f"{sum(fails)} of {len(rows)} don't have the requested {label[0]}",
                    tr=f"{len(rows)} yazıdan {sum(fails)} tanesinde istenen {label[1]} yok",
                )
            )
    if misses:
        return _receipt(req, "partial", "; ".join(misses))
    if not judged or undecided:
        return None
    return _receipt(req, "met", None)


_STATUS_RANK = {"met": 0, "partial": 1, "not_possible": 2}


def combine_receipts(first: RequirementReceipt, second: RequirementReceipt) -> RequirementReceipt:
    """The weaker of two verdicts on one requirement, with both reasons (KRI-558)."""
    worst = max((first, second), key=lambda r: _STATUS_RANK[r.status])
    reasons = [r.reason for r in (first, second) if r.reason and r.status != "met"]
    return worst.model_copy(update={"reason": "; ".join(dict.fromkeys(reasons))[:300] or None})


def _with_text_look(
    req: BriefRequirement, facts: PlanFacts, base: RequirementReceipt
) -> RequirementReceipt:
    """Add the look check to a text requirement's presence check (KRI-558)."""
    if not text_look_facts(req) or base.status != "met":
        return base
    look = check_text_look(req, facts)
    if look is None:
        # The words are there; whether they look as asked is not visible here (a record or
        # a draft). A presence check alone never certifies the look (KRI-524).
        return _receipt(req, "partial", _NO_CHECKER)
    return combine_receipts(base, look)


def _check_clip_lengths(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Make all clips 1 second except the first and last": judged on the clip lengths."""
    ask = clip_length_ask(req)
    durations = facts.clip_output_durations
    if ask is None or not durations:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    seconds, skip_first, skip_last = ask
    rows = list(durations)
    if skip_first:
        rows = rows[1:]
    if skip_last:
        rows = rows[:-1]
    if not rows:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    tolerance = max(0.1, 0.1 * seconds)
    off = [length for length in rows if abs(length - seconds) > tolerance]
    if not off:
        return _receipt(req, "met", None)
    shown = ", ".join(f"{length:.1f}s" for length in off[:3])
    return _receipt(
        req,
        "partial",
        say(
            en=f"{len(off)} of {len(rows)} clips aren't {seconds:g}s ({shown})",
            tr=f"{len(rows)} klipten {len(off)} tanesi {seconds:g} sn değil ({shown})",
        ),
    )


def describe_text_look(req: BriefRequirement, facts: PlanFacts) -> str | None:
    """What the video's texts actually use, for an ask no value check could decide."""
    ask = derive_style_ask(req)
    if ask is None or not facts.text_styles:
        return None
    rows = [
        row
        for row in facts.text_styles
        if ask.target in (None, "all_text") or row.kind == _TARGET_KIND[ask.target]
    ]
    parts = []
    for name in ask.wanted:
        counts: dict[str, int] = {}
        for row in rows:
            value = _row_value(row, name)
            if value:
                counts[value] = counts.get(value, 0) + 1
        if counts:
            shown = ", ".join(f"{v} ×{n}" if n > 1 else v for v, n in counts.items())
            field_name = say(en=_STYLE_FIELD_NAMES[name][0], tr=_STYLE_FIELD_NAMES[name][1])
            parts.append(f"{field_name}: {shown}")
    return "; ".join(parts)[:300] or None


def _check_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if clip_length_ask(req) is not None:
        # KRI-558: a per-clip length is judged on the clips, never against the total length.
        return _check_clip_lengths(req, facts)
    if _wants_whole_take(req):
        return _check_whole_take(req, facts)
    target = req.facts.get("duration_s")
    if _wants_whole_text_span(req):
        if not facts.editor or not facts.text_spans or facts.duration_s is None:
            return _receipt(req, "partial", _CANT_CHECK_TIMING)
        target_ids = req.facts.get("target_ids")
        if isinstance(target_ids, list) and target_ids:
            wanted = {str(value) for value in target_ids}
            spans = [
                span
                for span in facts.text_spans
                if span[0] in wanted and span[1] in {"title", "generative_intro"}
            ]
            if len(spans) != len(wanted):
                return _receipt(req, "partial", _CANT_CHECK_TIMING)
        else:
            if facts.text_timing_incomplete:
                return _receipt(req, "partial", _CANT_CHECK_TIMING)
            spans = [span for span in facts.text_spans if span[1] in {"title", "generative_intro"}]
        if not spans:
            return _receipt(req, "partial", _CANT_CHECK_TIMING)
        tolerance = 0.05
        if all(
            start <= tolerance and end >= facts.duration_s - tolerance for _, _, start, end in spans
        ):
            return _receipt(req, "met", None)
        return _receipt(req, "partial", "Some requested text ends before the video does.")
    if not isinstance(target, (int, float)) or target <= 0:
        if facts.narrated_steps is not None:
            return _check_narrated_timing(req, facts)
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    if req.scope != "global":
        # The total edit length cannot establish a clip or text duration. Until
        # that requirement has resolved target windows, report missing evidence
        # instead of rejecting a valid edit (or falsely passing an equal total).
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
    outro = ""
    if facts.outro_s:
        outro = say(
            en=f" (plus a {facts.outro_s:g}s outro on the finished video)",
            tr=f" (bitmiş videoda buna ek olarak {facts.outro_s:g} saniyelik bir kapanış var)",
        )
    return _receipt(
        req,
        "partial",
        say(
            en=(
                f"This draft is about {facts.duration_s:g}s{outro}; "
                f"you asked for {float(target):g}s."
            ),
            tr=(
                f"Bu taslak yaklaşık {facts.duration_s:g} saniye{outro}; sen "
                f"{float(target):g} saniye istedin."
            ),
        ),
    )


# ------------------------------------------- phone Voiceover render receipts (KRI-533)
#
# A phone Voiceover edit is laid out by the worker (`_run_phone_narrated_job`), which
# records every clip's window, the creator's labels for it and the words spoken over it
# (`assembly_plan["narrated_alignment"]`). These checkers read only that record, like the
# unified montage's do: an order anchor ("end on the sunset valley"), a timing ask that
# names a labelled clip group ("show the balloons while talking about the balloons") and
# the language of the captions. Anything the record cannot settle stays unchecked.

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an|my|our|that|this|these|those|some|bu)\s+")
_EXCERPT_CHARS = 110

_LANGUAGE_NAMES: dict[str, tuple[str, str]] = {
    "en": ("English", "İngilizce"),
    "tr": ("Turkish", "Türkçe"),
    "de": ("German", "Almanca"),
    "es": ("Spanish", "İspanyolca"),
    "fr": ("French", "Fransızca"),
    "it": ("Italian", "İtalyanca"),
    "ar": ("Arabic", "Arapça"),
    "nl": ("Dutch", "Felemenkçe"),
    "pt": ("Portuguese", "Portekizce"),
}
# Every spelling a creator types (diacritics stripped, see `loose_text`) -> language code.
_LANGUAGE_WORDS: dict[str, str] = {
    "english": "en",
    "ingilizce": "en",
    "turkish": "tr",
    "turkce": "tr",
    "german": "de",
    "almanca": "de",
    "spanish": "es",
    "ispanyolca": "es",
    "french": "fr",
    "fransizca": "fr",
    "italian": "it",
    "italyanca": "it",
    "arabic": "ar",
    "arapca": "ar",
    "dutch": "nl",
    "hollandaca": "nl",
    "felemenkce": "nl",
    "portuguese": "pt",
    "portekizce": "pt",
}
_LANG_ALT = "|".join(sorted(_LANGUAGE_WORDS, key=len, reverse=True))
_CAPTION_WORD = r"(?:sub\s*titles?|captions?|altyazi\w*)"
# Most specific first: "translate the captions to English" names the target outright.
_CAPTION_LANGUAGE_PATTERNS = (
    re.compile(rf"\btranslat\w*\s+(?:\w+\s+){{0,3}}?(?:in|into|to)\s+({_LANG_ALT})\b"),
    re.compile(rf"\b({_LANG_ALT})\s+{_CAPTION_WORD}"),
    re.compile(rf"\b{_CAPTION_WORD}\s+(?:in|into|to|as)\s+({_LANG_ALT})\b"),
    re.compile(rf"\baltyazi\w*\s+({_LANG_ALT})\b"),
    re.compile(rf"\b({_LANG_ALT})\s+(?:olarak|cevir\w*)"),
)
_SPELLING_RE = re.compile(r"\b(?:spell\w*|exact\w*|yazim\w*|dogru yaz\w*)\b")


def _loose_req(req: BriefRequirement) -> str:
    """The requirement's words, diacritic-free and punctuation-free, for name matching."""
    return " ".join(re.sub(r"[^\w\s]", " ", loose_text(_req_text(req))).split())


def _label_core(value: str) -> str:
    """A creator's name for a group ("The Balloons!") without case, accents or article."""
    text = " ".join(re.sub(r"[^\w\s]", " ", loose_text(value or "")).split())
    return _LEADING_ARTICLE_RE.sub("", text).strip()


def _same_group(label: str, words: str) -> bool:
    a, b = _label_core(label), _label_core(words)
    return bool(a and b) and (a == b or _contains_text(a, b) or _contains_text(b, a))


def _sentence_case(value: str) -> str:
    value = value.strip()
    return value[:1].upper() + value[1:]


def _step_names(step: NarratedStepFact) -> tuple[str, ...]:
    return (*step.labels, *(name for _spot, name in step.placed))


def _narrated_anchors(
    req: BriefRequirement, steps: Sequence[NarratedStepFact]
) -> list[tuple[str, str]]:
    """The (spot, words) first/last seats an order requirement asks for.

    Stated outright (``first_clip`` / ``last_clip``) or implied: a resolved ``first`` /
    ``last`` order intent whose name the requirement's own words contain.
    """
    anchors: list[tuple[str, str]] = []
    for key, spot, _verb in _ORDER_ANCHOR_FACTS:
        words = req.facts.get(key)
        if isinstance(words, str) and words.strip():
            anchors.append((spot, words.strip()))
    seated = {spot for spot, _words in anchors}
    wording = _loose_req(req)
    for step in steps:
        for spot, name in step.placed:
            core = _label_core(name)
            if spot in ("first", "last") and spot not in seated and core:
                if _contains_text(wording, core):
                    anchors.append((spot, name))
                    seated.add(spot)
    return anchors


def _check_narrated_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A stated first / last clip is met when that clip really opens / closes the edit."""
    steps = facts.narrated_steps or ()
    anchors = _narrated_anchors(req, steps)
    if not anchors or not steps:
        # No seat to judge: the voiceover sets this edit's order, so any other ordering
        # rule stays unverified, exactly as before the render recorded anything.
        return _receipt(req, "partial", _CANT_CHECK_ORDER_RULE)
    # Same strictness as the montage branch: only a bound / contract-stamped job turns a
    # missed seat into a failure; an unbound one keeps the softer "partly".
    unmet = "not_possible" if facts.strict_order and _order_is_required(req) else "partial"
    problems: list[str] = []
    unknown = False
    for spot, words in anchors:
        members = [
            index
            for index, step in enumerate(steps)
            if any(_same_group(name, words) for name in _step_names(step))
        ]
        if not members:
            unknown = True
            continue
        want = len(steps) - 1 if spot == "last" else 0
        if want in members:
            continue
        where = members[-1] if spot == "last" else members[0]
        name = _sentence_case(words)
        problems.append(
            say(
                en=f"{name} is clip {where + 1} of {len(steps)}, not the {spot} one.",
                tr=f"{name} {len(steps)} klipten {where + 1}. sırada, {_SPOT_TR[spot]} değil.",
            )
        )
    if problems:
        return _receipt(req, unmet, " ".join(problems))
    if unknown:
        return _receipt(req, "partial", _CANT_CONFIRM_ORDER)
    return _receipt(req, "met", None)


def _excerpt(text: str) -> str:
    """The first and last words of a narration, joined with an ellipsis when long."""
    text = " ".join(text.replace('"', "'").split())
    if len(text) <= _EXCERPT_CHARS:
        return text
    head = text[: int(_EXCERPT_CHARS * 0.55)].rsplit(" ", 1)[0]
    tail = text[-int(_EXCERPT_CHARS * 0.4) :].split(" ", 1)[-1]
    return f"{head} … {tail}"


def _wants_clip_timing(req: BriefRequirement) -> bool:
    """A timing ask with no number, no title and no "keep my take": it can only be about
    when something plays, which a rendered Voiceover record may be able to answer.
    A pop-in ask ("show my photo when voiceover says X", KRI-537) is the beat checker's,
    judged at draft from the strategy and at render from the variant's beat receipt, so
    it is neither deferred nor read off the clip windows here."""
    return (
        req.kind == "timing"
        and req.scope != "title"
        and not _has_duration_target(req)
        and not _wants_whole_take(req)
        and not _wants_beats(req)
    )


def _check_narrated_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """ "Show the balloons while talking about the balloons": report what the voiceover
    says over the clips the creator named. Only clips placed by the spoken words count."""
    steps = facts.narrated_steps or ()
    if facts.ordering_basis not in NARRATED_ALIGNED_BASES or not steps:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    wording = _loose_req(req)
    named: list[str] = []
    for step in steps:
        for name in _step_names(step):
            core = _label_core(name)
            if (
                core
                and _contains_text(wording, core)
                and not any(_same_group(name, n) for n in named)
            ):
                named.append(name)
    if not named:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    members = [
        index
        for index, step in enumerate(steps)
        if any(_same_group(name, wanted) for name in _step_names(step) for wanted in named)
    ]
    first, last = members[0], members[-1]
    if last - first + 1 != len(members):
        # The named clips are split apart: one window would claim the clips between them.
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    excerpt = _excerpt(" ".join(step.text for step in steps[first : last + 1] if step.text))
    if not excerpt:
        return _receipt(req, "partial", _CANT_CHECK_TIMING)
    start, end = steps[first].start_s, steps[last].end_s
    label = _sentence_case(" and ".join(named))
    return _receipt(
        req,
        "met",
        say(
            en=f'{label} clips play from {start:.1f} s to {end:.1f} s, under: "{excerpt}"',
            tr=f"{label} klipleri {start:.1f} sn ile {end:.1f} sn arasında oynuyor, "
            f'şu sözlerle: "{excerpt}"',
        ),
    )


def _requested_caption_language(req: BriefRequirement) -> str | None:
    """The language a captions ask wants ("English subtitles", "captions in Turkish"), as
    a code, else None. "English subtitles translated from the Turkish voiceover" is English."""
    wording = loose_text(_req_text(req))
    for pattern in _CAPTION_LANGUAGE_PATTERNS:
        found = pattern.search(wording)
        if found:
            return _LANGUAGE_WORDS[found.group(1)]
    return None


def _language_name(code: str) -> str:
    english, turkish = _LANGUAGE_NAMES.get(code, (code.upper(), code.upper()))
    return say(en=english, tr=turkish)


def _check_caption_language(
    req: BriefRequirement, facts: PlanFacts, asked: str
) -> RequirementReceipt:
    """The captions' language against the one the creator asked for. Spelling of names
    stays unchecked, and the reason says so when the ask was about spelling."""
    rendered = str(facts.caption_language or "").casefold().split("-")[0]
    note = ""
    if _SPELLING_RE.search(loose_text(_req_text(req))):
        note = " " + say(
            en="I haven't checked how the names are spelled.",
            tr="İsimlerin yazımını kontrol etmedim.",
        )
    if rendered != asked:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    f"The captions are in {_language_name(rendered)}, not {_language_name(asked)}."
                ),
                tr=(
                    f"Altyazılar {_language_name(asked)} değil, {_language_name(rendered)} "
                    "olarak çıktı."
                ),
            )
            + note,
        )
    spoken = str(facts.spoken_language or "").casefold().split("-")[0]
    if spoken and spoken != rendered:
        reason = say(
            en=(
                f"The captions are in {_language_name(rendered)}, translated from the "
                f"{_language_name(spoken)} voiceover."
            ),
            tr=(
                f"Altyazılar {_language_name(rendered)}, {_language_name(spoken)} "
                "seslendirmeden çevrildi."
            ),
        )
    else:
        reason = say(
            en=f"The captions are in {_language_name(rendered)}.",
            tr=f"Altyazılar {_language_name(rendered)}.",
        )
    return _receipt(req, "met", reason + note)


def asks_caption_language(req: BriefRequirement) -> bool:
    """True for a captions ask that names a language (judged on the rendered captions)."""
    return _wants_captions(req) and _requested_caption_language(req) is not None


# ------------------------------------------------------------- caption words (KRI-549)
# "altyazılar Türkçe olsun; Moda, Bahariye, Yeldeğirmeni ve Kadıköy doğru yazılsın" on a
# finished phone render: the language is judged against the variant's `caption_language`
# and each name the creator asked to be spelled right against the caption lines it shows.
# A name is read only from a capitalized list right next to a spelling cue (the creator's
# own spelling). Anything less certain stays unchecked: a false "Done" is worse than
# "can't check".

# Wordings the stricter `_CAPTION_LANGUAGE_PATTERNS` miss ("captions should be in English",
# "altyazılar da Türkçe", "altyazıları Türkçeye çevir"); run on `loose_text`.
_LOOSE_CAPTION_LANGUAGE_PATTERNS = (
    re.compile(rf"\b{_CAPTION_WORD}\s+(?:\w+\s+){{0,3}}?(?:in|into)\s+({_LANG_ALT})\b"),
    re.compile(rf"\baltyazi\w*\s+(?:\w+\s+){{0,2}}?({_LANG_ALT})\w*"),
)
# A spelling cue in the creator's own words (case kept, so the names stay readable). After
# "spell"/"spelling (of)" the names follow ("spell A and B right"); after any other cue
# they come first ("A ve B doğru yazılsın", "A and B spelled right"). A colon list
# ("spelling the names exactly: A, B") always follows.
_SPELL_CUE_RE = re.compile(
    r"\b(?P<after>spell(?:ing)?(?:\s+of)?)\b"
    r"|\b(?:spelled|spelt|spells)\b"
    r"|\b(?:do[gğ]ru|d[uü]zg[uü]n|hatas[ıi]z)\s+(?:bir\s+)?yaz\w*"
    r"|\byaz[ıi]m\w*|\bimla\w*",
    re.IGNORECASE,
)
_SPELL_CLAUSE_SPLIT_RE = re.compile(r"[;\n!?]+|\.(?=\s|$)")
# Words (inner apostrophes and hyphens kept: "Kadıköy'ü", "Yel-Değirmeni") and list commas.
_NAME_TOKEN_RE = re.compile(r"[^\W_](?:[\w'’\-]*[^\W_])?|[,&]")
_NAME_JOINERS = frozenset({"and", "ve", "ile", "&", "plus"})
_NAME_PARTICLES = frozenset(
    {"de", "da", "del", "della", "di", "du", "la", "le", "van", "von", "der", "den", "al", "el"}
)
# Capitalized words that open a sentence or name the captions, never a name to spell.
_NOT_NAMES = frozenset(
    "make please ensure also and the all keep write check use i my names"
    " lutfen ayrica ve tum butun ben benim yaz isimler isimleri".split()
)
_MAX_NAME_GAP = 3  # words allowed between a name list and its cue ("A and B are spelled")
_MAX_SPELLED_NAMES = 12
# Caption look, size or place (`_CAPTION_DETAIL_RE` without the language words and without a
# bare "right", which "spell it right" uses): such an ask stays with `_check_captions`.
_CAPTION_LOOK_RE = re.compile(
    r"\b(colou?rs?|yellow|white|red|blue|green|black|pink|orange|purple|lime|font|bold|italic"
    r"|sizes?|bigger|big|small|smaller|larger|huge|tiny|top|bottom|middle|cent(er|re)"
    r"|higher|lower|outline|shadow|stroke|uppercase|lowercase|caps)\b"
    r"|\b(on|to|at) the (left|right)\b|\b(left|right)[- ](side|aligned|corner)\b"
    r"|\brenk|\bsari\b|\bbeyaz\b|\bbüyük|\bküçük|\büst|\balt(ta|a)\b|\bsol(da|a)\b"
    r"|\bsağ(da|a)\b|\byazi tipi|\bkalin"
)
# Caption words: letters/digits joined by apostrophes ("Kadıköy'de"); hyphens and spaces split.
_CAPTION_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_APOSTROPHE_RE = re.compile(r"['’]")
# A Turkish case/possessive ending glued onto a name without its apostrophe ("Kadıköyde",
# "Modanın"), on `loose_text` letters. Only checked on a capitalized word of a long enough
# name, so "modası" (fashion) or "Adana" never reads as a misspelled Moda or Ada.
_GLUED_SUFFIX_RE = re.compile(r"[nys]?(?:[dt][ae](?:n|ki)?|[ae]|[iu]n?|l[ae]r\w{0,4}|l[iu]|l[ae])")
_MIN_GLUED_NAME = 4


def _asked_caption_language(req: BriefRequirement) -> str | None:
    """`_requested_caption_language`, plus a few looser wordings of the same ask."""
    found = _requested_caption_language(req)
    if found is not None:
        return found
    wording = loose_text(_req_text(req))
    for pattern in _LOOSE_CAPTION_LANGUAGE_PATTERNS:
        match = pattern.search(wording)
        if match:
            return _LANGUAGE_WORDS[match.group(1)]
    return None


def _capitalized(token: str) -> bool:
    return token[:1].isupper()


def _name_lists(text: str) -> tuple[list[str], list[tuple[int, int, list[str]]]]:
    """``text``'s tokens and its capitalized name lists as (first token, last token, names).

    A name is a run of capitalized words (a lowercase particle may join two: "Rio de
    Janeiro"); names joined by commas, "and", "ve", "ile" or "&" form one list.
    """
    tokens = _NAME_TOKEN_RE.findall(text)
    chunks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(tokens):
        if not _capitalized(tokens[i]):
            i += 1
            continue
        words, j = [tokens[i]], i + 1
        # A word with an apostrophe ends its name ("Kadıköy'ü Moda" is two names).
        while j < len(tokens) and not _APOSTROPHE_RE.search(words[-1]):
            if _capitalized(tokens[j]):
                words.append(tokens[j])
                j += 1
            elif (
                tokens[j].casefold() in _NAME_PARTICLES
                and j + 1 < len(tokens)
                and _capitalized(tokens[j + 1])
            ):
                words.extend(tokens[j : j + 2])
                j += 2
            else:
                break
        chunks.append((i, j - 1, " ".join(words)))
        i = j
    lists: list[tuple[int, int, list[str]]] = []
    for start, end, name in chunks:
        between = [token.casefold() for token in tokens[lists[-1][1] + 1 : start]] if lists else []
        joined = bool(lists) and (
            between == [","]
            or (
                len(between) in (1, 2)
                and between[-1] in _NAME_JOINERS
                and between[:-1] in ([], [","])
            )
        )
        if joined:
            lists[-1] = (lists[-1][0], end, [*lists[-1][2], name])
        else:
            lists.append((start, end, [name]))
    return tokens, lists


def _word_count(tokens: Sequence[str]) -> int:
    return sum(1 for token in tokens if token not in {",", "&"})


def _list_before(text: str, max_gap: int = _MAX_NAME_GAP) -> list[str]:
    tokens, lists = _name_lists(text)
    if lists and _word_count(tokens[lists[-1][1] + 1 :]) <= max_gap:
        return lists[-1][2]
    return []


def _list_after(text: str, max_gap: int = _MAX_NAME_GAP) -> list[str]:
    tokens, lists = _name_lists(text)
    if lists and _word_count(tokens[: lists[0][0]]) <= max_gap:
        return lists[0][2]
    return []


def _clean_name(name: str) -> str | None:
    """The name without a case ending ("Kadıköy'ü" -> "Kadıköy"), or None for a non-name."""
    words = _APOSTROPHE_RE.split(name, maxsplit=1)[0].split()
    while words and loose_text(words[0]) in _NOT_NAMES:
        words = words[1:]
    if not words:
        return None
    core = loose_text(" ".join(words))
    if core in _LANGUAGE_WORDS or _CAPTION_RE.search(core):
        return None
    return " ".join(words)


def _names_near_cue(clause: str, cue: re.Match[str]) -> list[str]:
    before, after = clause[: cue.start()], clause[cue.end() :]
    found: list[str] = []
    if ":" in after:
        found = _list_after(after.split(":", 1)[1], max_gap=1)
    if not found:
        order = (
            ((_list_after, after), (_list_before, before))
            if cue.group("after")
            else ((_list_before, before), (_list_after, after))
        )
        for pick, text in order:
            found = pick(text)
            if found:
                break
    return [name for name in map(_clean_name, found) if name]


def _spelled_names(req: BriefRequirement) -> tuple[str, ...] | None:
    """The names a requirement asks to be spelled right, as the creator wrote them.

    None when the wording has no spelling cue; () when it has one but no capitalized
    name list sits next to it (then nothing about spelling can be claimed).
    """
    text = unicodedata.normalize("NFC", " ".join(x for x in (req.description, req.literal) if x))
    cued = False
    names: list[str] = []
    for clause in _SPELL_CLAUSE_SPLIT_RE.split(text):
        for cue in _SPELL_CUE_RE.finditer(clause):
            cued = True
            names.extend(_names_near_cue(clause, cue))
    if not cued:
        return None
    return tuple(dict.fromkeys(names))[:_MAX_SPELLED_NAMES]


def _asks_spelling(req: BriefRequirement) -> bool:
    return _spelled_names(req) is not None or bool(_SPELLING_RE.search(_loose_req(req)))


def _caption_words_ask(req: BriefRequirement) -> bool:
    """A captions ask about their language and/or how names are spelled, and nothing about
    their look or place (`_check_captions` keeps those)."""
    if _wants_no_captions(req):
        return False
    if _asked_caption_language(req) is None and not _asks_spelling(req):
        return False
    look = _req_text(req)
    for name in _spelled_names(req) or ():
        look = look.replace(_fold(name), " ")
    return not _CAPTION_LOOK_RE.search(look)


def _judges_caption_words(req: BriefRequirement, facts: PlanFacts) -> bool:
    """True when a finished render's caption lines can settle this captions ask."""
    return facts.caption_texts is not None and _wants_captions(req) and _caption_words_ask(req)


def _spelling_key(value: str, *, turkish: bool) -> str:
    """Case-free, whitespace-collapsed text that keeps every letter ("İ" -> "i"; in Turkish
    "I" -> "ı"), so "KADIKÖY" equals "Kadıköy" but "Kadikoy" does not."""
    text = unicodedata.normalize("NFC", value).replace("İ", "i")
    if turkish:
        text = text.replace("I", "ı")
    return " ".join(text.casefold().split())


def _squash(value: str) -> str:
    """Letters only, diacritic-free: "Yel-Değirmeni" == "yeldegirmeni"."""
    return re.sub(r"[\W_]+", "", loose_text(value))


def _caption_spelling(name: str, captions: str, *, turkish: bool) -> tuple[bool, str | None]:
    """(``name`` appears as written, the first other spelling of it in ``captions``).

    A Turkish ending after an apostrophe is allowed ("Kadıköy'de"). Another spelling is the
    same letters with other accents, spacing or hyphens ("Kadikoy", "yel değirmeni"), or
    the name with an ending glued on without its apostrophe ("Kadıköyde").
    """
    want = _squash(name)
    if not want:
        return False, None
    want_key = _spelling_key(name, turkish=turkish)
    name_words = len(re.split(r"[\s\-]+", name.strip()))
    tokens = list(_CAPTION_TOKEN_RE.finditer(captions))
    exact = False
    other: str | None = None
    for i in range(len(tokens)):
        for size in range(1, name_words + 2):
            window = tokens[i : i + size]
            if len(window) < size:
                break
            if size > 1:
                # A name never runs past a case ending or punctuation ("Moda'da, deniz").
                gap = captions[window[-2].end() : window[-1].start()]
                if _APOSTROPHE_RE.search(window[-2].group()) or not re.fullmatch(r"[\s\-]+", gap):
                    break
            last = window[-1].group()
            base = _APOSTROPHE_RE.split(last, maxsplit=1)[0]
            shown = captions[window[0].start() : window[-1].start() + len(base)]
            squashed = _squash(shown)
            if squashed == want:
                if _spelling_key(shown, turkish=turkish) == want_key:
                    exact = True
                elif other is None:
                    other = shown
            elif (
                other is None
                and size == name_words
                and base == last
                and len(want) >= _MIN_GLUED_NAME
                and _capitalized(shown)
                and squashed.startswith(want)
                and _GLUED_SUFFIX_RE.fullmatch(squashed[len(want) :])
            ):
                other = shown
    return exact, other


def _and_names(names: Sequence[str]) -> str:
    items = list(dict.fromkeys(names))
    if len(items) <= 1 or len(items) > _MAX_NAMED_IN_REASON:
        return _names(items)
    head = ", ".join(items[:-1])
    return say(en=f"{head} and {items[-1]}", tr=f"{head} ve {items[-1]}")


def _check_caption_words(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """A captions ask about their language and the spelling of names, on a finished render.

    The language is judged against the render's ``caption_language``, each name against
    the caption lines. Met only when every part holds; a wrong language or a different
    spelling is an honest miss naming it, a name the captions never show is "Partly"
    with that reason, and a part with nothing to judge it by leaves the ask unchecked.
    """
    lines = facts.caption_texts or ()
    if not lines:
        return _receipt(
            req, "partial", say(en="This video has no captions.", tr="Bu videoda altyazı yok.")
        )
    done: list[str] = []
    misses: list[str] = []
    unsure: list[str] = []
    rendered = str(facts.caption_language or "").casefold().split("-")[0]
    asked = _asked_caption_language(req)
    if asked is not None:
        if not rendered:
            unsure.append(
                say(
                    en="I couldn't check which language the captions are in.",
                    tr="Altyazıların dilini kontrol edemedim.",
                )
            )
        elif rendered != asked:
            misses.append(
                say(
                    en=(
                        f"The captions are in {_language_name(rendered)}, "
                        f"not {_language_name(asked)}."
                    ),
                    tr=(
                        f"Altyazılar {_language_name(asked)} değil, {_language_name(rendered)} "
                        "olarak çıktı."
                    ),
                )
            )
        else:
            done.append(
                say(
                    en=f"The captions are in {_language_name(rendered)}.",
                    tr=f"Altyazılar {_language_name(rendered)}.",
                )
            )
    if _wants_word_captions(req) and facts.caption_style not in {"karaoke", "kinetic"}:
        misses.append(
            say(
                en="Captions are on as full sentences, not word by word.",
                tr="Altyazılar kelime kelime değil, tam cümleler olarak açık.",
            )
        )
    names = _spelled_names(req)
    if _asks_spelling(req):
        if not names:
            unsure.append(
                say(
                    en="I couldn't tell which words to check the spelling of.",
                    tr="Yazımını kontrol edeceğim kelimeleri ayırt edemedim.",
                )
            )
        else:
            captions = " ".join(lines)
            turkish = rendered == "tr" or (not rendered and bool(re.search("[ıİşŞğĞ]", captions)))
            spelled: list[str] = []
            wrong: list[tuple[str, str]] = []
            absent: list[str] = []
            for name in names:
                exact, other = _caption_spelling(name, captions, turkish=turkish)
                if other is not None:
                    wrong.append((name, other))
                elif exact:
                    spelled.append(name)
                else:
                    absent.append(name)
            if wrong:
                misses.append(
                    say(
                        en="The captions write "
                        + ", ".join(f'"{other}" for {name}' for name, other in wrong)
                        + ". You can fix that in the editor.",
                        tr="Altyazılarda "
                        + ", ".join(f'{name} yerine "{other}"' for name, other in wrong)
                        + " yazıyor. Bunu editörde düzeltebilirsin.",
                    )
                )
            if absent:
                one = len(absent) == 1
                misses.append(
                    say(
                        en=(
                            f"{_and_names(absent)} {'doesn' if one else 'don'}'t appear in "
                            f"the captions, so I couldn't check {'its' if one else 'their'} "
                            "spelling."
                        ),
                        tr=(
                            f"Altyazılarda {_and_names(absent)} geçmiyor, o yüzden "
                            f"{'yazımını' if one else 'yazımlarını'} kontrol edemedim."
                        ),
                    )
                )
            if spelled:
                one = len(spelled) == 1
                done.append(
                    say(
                        en=(
                            f"{_and_names(spelled)} {'is' if one else 'are'} spelled as you "
                            f"wrote {'it' if one else 'them'}."
                        ),
                        tr=f"{_and_names(spelled)} yazdığın gibi yazılmış.",
                    )
                )
    if misses:
        return _receipt(req, "partial", " ".join([*misses, *unsure, *done]))
    if unsure:
        return _receipt(req, "partial", " ".join([*unsure, *done]), verification="unchecked")
    return _receipt(req, "met", " ".join(done) or None)


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
    return _receipt(
        req,
        "partial",
        say(
            en="I used a plain default title because none was given.",
            tr="Başlık vermediğin için sade bir varsayılan başlık kullandım.",
        ),
    )


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

    blocked = say(
        en=NO_TITLE_BLOCKED,
        tr="Videoyu henüz oluşturamadım: başlık istedin ama kelimelerini vermedin.",
    )
    alternative = say(
        en=TITLE_WORDS_ALTERNATIVE,
        tr='Başlığın kelimelerini söyle ya da "başlık olmasın" yaz.',
    )
    fallback = say(en="A requested change is missing.", tr="İstediğin bir değişiklik eksik.")
    reasons = [str(row.get("reason") or fallback) for row in failures]
    if reasons and all(is_no_title_reason(reason) for reason in reasons):
        saved = say(en="Your draft is saved.", tr="Taslağın kaydedildi.")
        return RenderBlockRecovery(
            message=f"{blocked} {saved} {alternative}",
            decline_reason="needs_choice",
            field_path="opening_title",
            alternative=alternative,
        )
    shown = " ".join(dict.fromkeys(blocked if is_no_title_reason(r) else _loc(r) for r in reasons))
    suffix = say(
        en=_RENDER_BLOCK_SUFFIX,
        tr="Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa isteği sadeleştireyim mi?",
    )
    message = f"{shown} {suffix}"
    if any(is_no_title_reason(r) for r in reasons):
        # Untyped (two blockers), but the title's way forward must not be lost.
        message = f"{message} " + say(
            en=_TITLE_WAY_FORWARD_MIXED,
            tr='Başlık için kelimeleri söyle ya da "başlık olmasın" yaz.',
        )
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
    # Chapter lists are satisfied by their separate labels, not a joined title.
    if found or chapter_list(req.literal, facts.texts) is not None:
        # Copy presence cannot certify independently requested visual/temporal behavior.
        # Keep these explicit constraints unverified until their actual lane evidence is checked.
        # (animation, position, font_family and text_color are judged by `check_text_look`.)
        if set(req.facts or {}) & {
            "segmentation",
            "sequence",
            "timing",
            "overlap",
            "animation_phases",
            "duration_s",
        }:
            return _receipt(req, "partial", _NO_CHECKER)
        return _receipt(req, "met", None)
    return _receipt(
        req,
        "partial",
        say(en="That exact text isn't in this draft.", tr="Bu tam yazı bu taslakta yok."),
    )


# KRI-546: two asks a finished phone montage can settle from its own evidence. Patterns run
# on `_loose_req` output (folded, no diacritics, punctuation as spaces: "don't" -> "don t").
_MEDIA_NOUN = (
    r"(?:videos?|clips?|shots?|footage|files?|uploads?|video\w*|klip\w*|cekim\w*|dosya\w*)"
)
# "If the same video is there twice, use one" / "aynı videodan iki tane varsa birini kullan":
# a copy is named AND only one should stay, so "use the same clip at the start and the end"
# never reads as a duplicate ask.
_SAME_FILE_RE = re.compile(
    r"\bduplicat\w*|\bde ?dup\w*|\bkopya\w*|\byinelen\w*|\btekrar ?(?:eden|lanan)\b"
    rf"|\bsame {_MEDIA_NOUN}|\b(?:two|2) of the same\b"
    rf"|\b{_MEDIA_NOUN} (?:that )?(?:is|are) the same\b|\bayni {_MEDIA_NOUN}"
)
_KEEP_ONE_RE = re.compile(
    r"\b(?:remove|drop|skip|delete|cut|avoid|exclude|leave out|get rid of|no|without|don t"
    r"|do not|never|once|single)\b|\b(?:only|just|keep|use|leave) (?:one|a single)\b"
    r"|\bone (?:of (?:them|each|the)|copy)\b"
    r"|\bbiri(?:ni|sini)?\b|\btek\b|\bsadece\b|\byalnizca\b|\bcikar(?:t|in|tin|sin|tsin)?\b"
    r"|\bsil(?:in|sin)?\b|\bkullanma(?:yin|sin)?\b|\bolmasin\b|\bbir (?:kere|kez)\b"
)
# "End on Elif's sentence to the camera, in her own voice" / "en sonda Elif'in kameraya
# söylediği cümleyi kendi sesiyle kullan": the closing clip's own spoken line.
_ENDING_RE = re.compile(
    r"\b(?:finish|end|close|wrap up|wrap) (?:on|with)\b|\bending (?:on|with)\b"
    r"|\bat the (?:very )?end\b|\bin the end\b|\blast\b|\bclosing\b"
    r"|\ben son\w*|\bsonda\b|\bsonunda\b|\bsona\b|\bkapanis\w*|\bbitir\w*|\bbitsin\b"
)
_OWN_SPEECH_RE = re.compile(
    r"\bown (?:voice|sound|audio|words)\b|\b(?:her|his|their|my|your) voice\b"
    r"|\b(?:sentence|says|said|saying|speaks|speaking|talks|talking)\b|\bto (?:the )?camera\b"
    r"|\bkendi ses\w*|\bcumle\w*|\bsoyledi\w*|\bsoyler\w*|\bkonus\w*|\bdedi\w*|\bkameraya\b"
)
# A closing photo, sticker or song, a muted ending or a voiceover is a different ask.
_NOT_OWN_SPEECH_RE = re.compile(
    r"\bmute\w*|\bsilent\b|\bno sound\b|\bwithout (?:sound|audio)\b|\bsessiz\w*|\bkapat\w*"
    r"|\bvoice ?over\b|\bnarrat\w*|\bseslendirme\w*|\bdis ses\w*"
    r"|\bmusic\b|\bsong\b|\bmuzik\w*|\bsarki\w*"
    r"|\b(?:photo|picture|image|pic|sticker|stamp|badge|logo|emoji)s?\b"
    r"|\bfotograf\w*|\bresim\w*|\bgorsel\w*|\bcikartma\w*"
)


def _wants_one_of_duplicates(req: BriefRequirement) -> bool:
    """ "If the same video is there twice, use one": keep a single copy of a repeated file."""
    if req.kind not in ("select", "style"):
        return False
    text = _loose_req(req)
    return bool(_SAME_FILE_RE.search(text) and _KEEP_ONE_RE.search(text))


def _wants_closing_speech(req: BriefRequirement) -> bool:
    """ "End on X's sentence in her own voice": the last clip plays its own spoken line."""
    if req.kind not in (*_BEAT_KINDS, "order"):
        return False
    text = _loose_req(req)
    if _NOT_OWN_SPEECH_RE.search(text) or not _OWN_SPEECH_RE.search(text):
        return False
    return bool(
        _ENDING_RE.search(text)
        or req.facts.get("last_clip")
        or str(req.facts.get("position") or "").casefold() == "last"
    )


def judged_at_render(req: BriefRequirement) -> bool:
    """True for an ask only a finished phone montage's evidence settles (KRI-546).

    A unified montage's plan record cannot see these: a closing line is held while the
    plan is laid out, and a duplicate file is told by the upload fingerprints. The
    render-ready review judges them again even when the record carries a receipt.
    """
    return (
        _wants_one_of_duplicates(req)
        or _wants_closing_speech(req)
        # KRI-558: a look (title animation, label corner, font, colour) and a per-clip length
        # are read off the finished text lane and timeline, which a plan record never has.
        or bool(text_look_facts(req))
        or clip_length_ask(req) is not None
    )


_CLIP_ID_PART_RE = re.compile(r"[^0-9A-Za-z]+")


def _scope_clip_id(req: BriefRequirement, ids: Iterable[str]) -> str | None:
    """The clip a ``clip:<id>`` scope names among ``ids``, else None.

    The brief may name a clip by a short form of its id ("F0CECCF8" for
    "analysis-proxy-ios-F0CECCF8-5871-....mp4"): a whole id part of 6+ letters and digits
    that exactly one clip carries counts; anything ambiguous names no clip.
    """
    if not req.scope.startswith("clip:"):
        return None
    token = req.scope.split(":", 1)[1].strip()
    known = list(dict.fromkeys(ids))
    if token in known:
        return token
    if len(token) < 6 or not token.isalnum():
        return None
    folded = token.casefold()
    hits = [
        media_id
        for media_id in known
        if folded in {part.casefold() for part in _CLIP_ID_PART_RE.split(media_id)}
    ]
    return hits[0] if len(hits) == 1 else None


def _clip_numbers(positions: Sequence[int]) -> str:
    """[2, 3] -> "clips 2 and 3" / "2. ve 3. klipler"."""
    shown = [str(p) for p in positions]
    head, tail = shown[:-1], shown[-1]
    return say(
        en=f"clips {', '.join(head)} and {tail}" if head else f"clip {tail}",
        tr=(f"{', '.join(f'{p}.' for p in head)} ve {tail}. klipler" if head else f"{tail}. klip"),
    )


def _check_duplicates(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Judge "keep one of a repeated video" on the finished edit's clips (KRI-546).

    Met only when no two clips of the edit come from the same original file (same upload
    fingerprint). Without a fingerprint for every clip nothing is claimed.
    """
    groups = facts.duplicate_clip_positions
    if not facts.rendered_montage or groups is None:
        return _receipt(req, "partial", _NO_CHECKER)
    if not groups:
        return _receipt(
            req,
            "met",
            say(
                en="No two clips in the edit come from the same video file",
                tr="Düzenlemedeki hiçbir klip aynı video dosyasından gelmiyor",
            ),
        )
    where = "; ".join(_clip_numbers(group) for group in groups)
    extra = sum(len(group) - 1 for group in groups)
    return _receipt(
        req,
        # Some uploaded copies were left out but not all: partly done. None were: not done.
        "partial" if (facts.source_duplicate_copies or 0) > extra else "not_possible",
        say(
            en=f"The same video is in the edit more than once: {where}",
            tr=f"Aynı video düzenlemede birden fazla kez var: {where}",
        ),
    )


def _check_closing_speech(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    """Judge "end on X's spoken line in her own voice" on a finished montage (KRI-546).

    Met only when the clip the creator named (or, unnamed, the clip their own "last" ask
    seated) ends the edit and the plan held its whole spoken line with the camera audio
    kept, as the finished timeline shows.
    """
    ids = facts.clip_ids
    if not facts.rendered_montage or not ids:
        return _receipt(req, "partial", _NO_CHECKER)
    if req.scope.startswith("clip:"):
        target = _scope_clip_id(req, (*ids, *facts.source_clip_ids))
        if target is None:
            return _receipt(req, "partial", _NO_CHECKER)
        if target not in ids:
            return _receipt(
                req,
                "not_possible",
                say(en="That clip isn't in the edit", tr="O klip düzenlemede yok"),
            )
        if ids[-1] != target:
            return _receipt(
                req,
                "partial",
                say(
                    en="That clip is in the edit, but not at the end",
                    tr="O klip düzenlemede var ama sonda değil",
                ),
            )
    elif facts.closing_speech is None and "last" not in (facts.sequence_spots_met or ()):
        # Unnamed, and no "last" ask of the creator's landed: nothing says which clip.
        return _receipt(req, "partial", _NO_CHECKER)
    if facts.source_audio_kept is False:
        return _receipt(
            req,
            "partial",
            say(
                en="The video ends on that clip, but the clips' own sound is off",
                tr="Video o klipte bitiyor ama kliplerin kendi sesi kapalı",
            ),
        )
    if facts.closing_speech is None or facts.closing_speech.media_id != ids[-1]:
        return _receipt(
            req,
            "partial",
            say(
                en=(
                    "The video ends on that clip, but I couldn't confirm its whole spoken "
                    "line plays in its own sound"
                ),
                tr=(
                    "Video o klipte bitiyor ama cümlenin tamamının klibin kendi sesiyle "
                    "duyulduğunu doğrulayamadım"
                ),
            ),
        )
    return _receipt(
        req,
        "met",
        say(
            en="The video ends on that clip's whole spoken line, in its own sound",
            tr="Video, o klipteki cümlenin tamamıyla ve klibin kendi sesiyle bitiyor",
        ),
    )


def _has_checker(req: BriefRequirement) -> bool:
    """True when ``check_requirement`` can actually verify this requirement."""
    if _wants_cleanup(req) or _wants_captions(req):
        return True
    if judged_at_render(req):
        # Neutral (`_NO_CHECKER`) anywhere but a finished phone montage (KRI-546).
        return True
    if req.kind == "text":
        return bool(
            req.scope in ("per_clip", "title") or req.scope.startswith("clip:") or req.literal
        )
    if req.kind == "timing":
        # "Fast but readable" has no number to check: that is "can't verify"
        # (neutral in the reply), not a failed requirement. "Keep my whole take"
        # is checkable against the edit format and clip count.
        return (
            _has_duration_target(req)
            or _wants_whole_take(req)
            or _wants_whole_text_span(req)
            or _wants_beats(req)
            or _wants_clip_timing(req)
        )
    if derive_style_ask(req) is not None:
        return True
    if req.kind in _BEAT_KINDS:
        return (
            _wants_beats(req)
            or _wants_closing(req)
            or _wants_speech(req)
            or _wants_bed_under_voice(req)
            or _wants_bed_muted(req)
        )
    return req.kind == "order"


def check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if (
        _wants_cleanup(req)
        and facts.edit_format in _SPEECH_FORMATS
        and (not facts.editor or facts.rendered_variant)
    ):
        # "Cut out the long pauses ... keep it under 45 s" on a Talking or voiceover
        # edit: the cleanup ask owns the sentence; the length it names is judged
        # inside it. Elsewhere the sentence takes its kind's usual path.
        return _check_speech_cleanup(req, facts)
    if facts.rendered_montage:
        # KRI-546: a finished phone montage shows which files repeat and whether the
        # closing clip's own line plays whole; every other plan keeps its usual path.
        if _wants_one_of_duplicates(req):
            return _check_duplicates(req, facts)
        if _wants_closing_speech(req):
            if req.kind == "order":
                # "End on Elif saying it": the seat is an order verdict first.
                seated = _check_order(req, facts)
                if seated.status != "met":
                    return seated
            return _check_closing_speech(req, facts)
    if req.kind == "text":
        if req.scope == "per_clip" or req.scope.startswith("clip:"):
            return _with_text_look(req, facts, _check_per_clip_text(req, facts))
        if req.literal:
            return _with_text_look(req, facts, _check_literal_text(req, facts))
        if req.scope == "title":
            return _with_text_look(req, facts, _check_title(req, facts))
    elif req.kind == "order":
        return _check_order(req, facts)
    elif req.kind == "timing":
        if _wants_beats(req):
            return _check_reaction_beats(req, facts)
        return _check_timing(req, facts)
    elif (
        req.kind == "style"
        and facts.editor
        # A rendered variant keeps `editor=True` for its text lanes but also carries the
        # render's own pop-in receipt and mix level (`plan_facts_from_phone_variant`): judge
        # those asks.
        and not (facts.reaction_beats is not None and (_wants_beats(req) or _wants_closing(req)))
        and not (_wants_bed_under_voice(req) or _wants_bed_muted(req))
        # KRI-541: "clean captions" on a rendered variant is judged against its captions.
        and not (facts.rendered_variant and _wants_captions(req))
    ):
        return _check_style(req, facts)
    elif req.kind in _BEAT_KINDS and _wants_speech(req) and facts.speech_sections is not None:
        return _check_speech_excerpts(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_beats(req) or _wants_closing(req)):
        return _check_reaction_beats(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_bed_under_voice(req) or _wants_bed_muted(req)):
        return _check_voice_bed(req, facts)
    if _judges_caption_words(req, facts):  # KRI-549: language / names on a finished render
        return _check_caption_words(req, facts)
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


def defers_to_narrated_render(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft will render through the phone Voiceover compiler (KRI-533).

    Mirrors the worker's dispatch: a narrated-family format with the voiceover lane
    (`audio_strategy == "voiceover"`, i.e. a recorded voiceover; self-narration goes to
    the subtitled / Talking writers), phone clips, an enrolled account and a phone
    deployment that renders narrated edits now. That writer records
    `assembly_plan["narrated_alignment"]` and judges first/last order, clip timing and
    caption language from it, so the text-free draft must not.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415
    from app.services.phone_rollout import phone_render_supported_formats  # noqa: PLC0415

    fmt = str(edit_format or "")
    return bool(
        str(audio_strategy or "") == "voiceover"
        and fmt in NARRATED_EDIT_FORMATS
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
        and fmt in phone_render_supported_formats()
    )


def _settled_by_narrated_render(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """The asks a rendered Voiceover record can judge and a draft cannot (see above)."""
    if req.kind == "order":
        # A strategy that already names its ordering basis can judge a basis ask itself.
        return not strategy.get("ordering_basis")
    if req.kind == "timing":
        return _wants_clip_timing(req)
    return req.kind == "style" and asks_caption_language(req)


# KRI-549: formats the phone Talking writer (`_run_phone_subtitled_job`) renders when the
# creator's own speech is the spine (a self-narrated Voiceover item lands there too).
_PHONE_CAPTION_FORMATS = frozenset({"subtitled", "talking_head", *NARRATED_EDIT_FORMATS})


def defers_caption_words_to_phone_render(
    *,
    creator_id: object,
    edit_format: object,
    audio_strategy: object,
    clip_paths: Iterable[object] = (),
) -> bool:
    """True when this draft renders through the phone Talking writer (KRI-549).

    That writer persists every caption line and the captions' language on the variant, so
    the render-ready review judges a caption-language or name-spelling ask a text-free draft
    cannot. Mirrors the dispatch gate: phone clips, an enrolled account, phone `subtitled`
    rendering on, and no voiceover lane (a recorded voiceover goes to the Voiceover writer).
    A draft the gate then refuses never renders, so leaving the ask to the render costs
    nothing there.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415
    from app.services.phone_rollout import phone_render_supported_formats  # noqa: PLC0415

    return bool(
        str(edit_format or "") in _PHONE_CAPTION_FORMATS
        and str(audio_strategy or "") != "voiceover"
        and any(is_analysis_proxy_path(str(path)) for path in clip_paths or ())
        and settings.phone_rendering_for(creator_id)
        and "subtitled" in phone_render_supported_formats()
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
    plus guessed names) follow. A phone Voiceover draft likewise leaves its first/last
    order, clip-timing and caption-language asks to the render's record (KRI-533), and a
    phone Talking draft its caption-language / name-spelling asks (KRI-549).
    Otherwise this is the unchanged, full list.
    """
    strategy = strategy or {}
    clip_paths = list(clip_paths or ())
    edit_format = strategy.get("edit_format") or item_edit_format
    defers = defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    narrated = not defers and defers_to_narrated_render(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    phone_captions = defers_caption_words_to_phone_render(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    return [
        req
        for req in requirements
        if not (defers and req.kind in UNIFIED_SETTLED_KINDS)
        and not (narrated and _settled_by_narrated_render(req, strategy))
        and not (phone_captions and _wants_captions(req) and _caption_words_ask(req))
    ]


def is_judged(req: BriefRequirement | None, receipt: RequirementReceipt) -> bool:
    """True when a checker had what it needed to decide ``req``.

    With no checker, or with a neutral reason (the facts were missing), nothing
    was checked. Such a receipt read "Partly: add captions (I can't verify this
    one automatically yet)", a half-done claim on nearly every reply. Receipts
    stored before these stopped being written still exist, so every reader of
    stored receipts filters through this too.
    """
    if req is not None and receipt.verification == "checked" and receipt.stage == "applied":
        # KRI-558: a change proven by the before/after edit diff, not by a value check.
        return receipt.reason not in _NEUTRAL_REASONS
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
_LABEL_TR = {
    "met": "Yapıldı",
    "partial": "Kısmen",
    "not_possible": "Yapamadım",
    "chosen": "Senin seçimin",
}


def _label(status: str) -> str:
    return say(en=_LABEL[status], tr=_LABEL_TR[status])


# What the render says about each requested group, label or chapter text
# (`unified_montage._intent_outcomes`) is English; a Turkish reply reads these known
# phrases in Turkish and leaves anything else (the creator's own words) as written.
_OUTCOME_REASON_TR = {
    "I found no clips of it": "Bununla ilgili klip bulamadım",
    "I couldn't put your own words on it": "Senin sözlerini üstüne koyamadım",
    "no clip got one": "hiçbir klibe gelmedi",
    "I couldn't tell which clips": "Hangi kliplerden söz ettiğini anlayamadım",
    "its clips are already in an earlier group": "klipleri zaten önceki bir grupta",
    "the clips did not end up there": "klipler oraya gelmedi",
    "you chose grouping first, each group in the order you filmed it": (
        "önce gruplamayı seçtin, her grup çektiğin sırada"
    ),
}
_OUTCOME_STRETCH_PREFIX_TR = (
    ("you chose strictly chronological order, so ", "kesin kronolojik sırayı seçtin, o yüzden "),
    ("clips stay in the order you filmed them, so ", "klipler çektiğin sırada kalıyor, o yüzden "),
)
_OUTCOME_STRETCHES = re.compile(r"^(.+) is in (\d+) stretches$")


def _outcome_name(row: Mapping[str, Any]) -> str:
    name = str(row["name"])
    if current_reply_language() != "tr":
        return name
    op = row.get("op")
    if op == "order":
        position = str(row.get("position") or "")
        if position in _SPOT_TR and name.startswith(f"{position}: "):
            return f"{_SPOT_TR[position]}: {name[len(position) + 2 :]}"
        if name == "the order you described":
            return "tarif ettiğin sıra"
    elif op == "group" and name.startswith("group by "):
        return f"gruplama: {name[len('group by ') :]}"
    elif op == "caption" and name.startswith("text for "):
        return f"{name[len('text for ') :]} için yazı"
    elif op == "label" and name.startswith("the ") and name.endswith(" name on its clips"):
        return f"kliplerindeki {name[len('the ') : -len(' name on its clips')]} adı"
    return name


def _outcome_reason(reason: str) -> str:
    if current_reply_language() != "tr":
        return reason
    known = _OUTCOME_REASON_TR.get(reason)
    if known is not None:
        return known
    for prefix, prefix_tr in _OUTCOME_STRETCH_PREFIX_TR:
        if reason.startswith(prefix):
            parts = []
            for part in reason[len(prefix) :].split("; "):
                match = _OUTCOME_STRETCHES.match(part)
                parts.append(
                    f"{match.group(1)} {match.group(2)} parçaya bölünüyor" if match else part
                )
            return prefix_tr + "; ".join(parts)
    return _loc(reason)


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

    Requirements no checker could judge (``unchecked``) are never reported as a failure
    (KRI-558): the reply lists them under "Have a look at these in the video", and an
    editor turn never reaches that branch because its changes are named from the edit diff
    (`kria.editor_receipts`).
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
        line = f"{_label(status)}: {_outcome_name(row)}"
        if row.get("reason"):
            line += f" ({_outcome_reason(str(row['reason'])).rstrip('.')})"
        lines.append(line)
    for receipt in judged:
        line = f"{_label(receipt.status)}: {by_id[receipt.requirement_id].text()}"
        if receipt.reason:
            line += f" ({_loc(receipt.reason).rstrip('.')})"
        lines.append(line)
        guesses.extend(receipt.inferred)
    if guesses:
        shown = ", ".join(dict.fromkeys(guesses))
        lines.append(
            say(
                en=(
                    f"I guessed these, tell me if any is wrong: {shown} "
                    "(text I took from the footage, not from your words)"
                ),
                tr=(
                    f"Şunları tahmin ettim, yanlış olan varsa söyle: {shown} "
                    "(senin sözlerinden değil, çekimlerden aldığım yazılar)"
                ),
            )
        )
    body = "\n".join(f"- {line}" for line in lines)
    if unchecked:
        look_lines = []
        for receipt in unchecked:
            line = f"- {by_id[receipt.requirement_id].text()}"
            if receipt.reason and receipt.reason not in _NEUTRAL_REASONS:
                line += f" ({_loc(receipt.reason).rstrip('.')})"
            look_lines.append(line)
        header = say(en="Have a look at these in the video:", tr="Bunlara videoda bir göz at:")
        text = "\n".join(part for part in (body, header, "\n".join(look_lines)) if part)
        if notices:
            text += "\n" + " ".join(notices)
    elif failed or any(r.status != "met" for r in judged):
        text = (
            say(
                en="Not everything you asked for made it in:\n",
                tr="İstediklerinin hepsi videoya giremedi:\n",
            )
            + body
        )
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
    "ClosingSpeechFact",
    "NO_TITLE_REASON",
    "RenderBlockRecovery",
    "NARRATED_ALIGNED_BASES",
    "NARRATED_ALIGNMENT_FIELD",
    "NarratedStepFact",
    "asks_caption_language",
    "defers_caption_words_to_phone_render",
    "defers_to_narrated_render",
    "defers_to_unified_montage",
    "requirements_to_check_at_draft",
    "PlanFacts",
    "build_receipts",
    "check_requirement",
    "is_format_limit",
    "is_judged",
    "is_no_title_reason",
    "judged_at_render",
    "needs_creator_choice",
    "SpeechSectionFact",
    "plan_facts_from_editor_payload",
    "plan_facts_from_narrated_alignment",
    "plan_facts_from_phone_variant",
    "plan_facts_from_rendered_montage",
    "plan_facts_from_speech_montage",
    "plan_facts_from_strategy",
    "plan_facts_from_unified_montage",
    "render_block_recovery",
    "reply_from_receipts",
]
