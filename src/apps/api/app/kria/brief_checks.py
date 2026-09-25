"""Deterministic per-requirement checks and receipt-driven replies (KRI-188).

No model is involved. Each checker compares one Creative Brief requirement with
facts read from the drafted plan (a strategy or an editor payload) and returns a
``RequirementReceipt``. A requirement with no checker is reported ``partial``
("could not be verified"): the reply never claims what the server did not check.

Checks implemented: per-clip text coverage, ordering vs the requested key,
duration within +/-10%, literal on-screen text, "keep my whole take" on a
single-clip subtitled edit, and word-triggered pop-ins (reaction beats) plus the
closing shot on a phone Talking edit.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt

if TYPE_CHECKING:
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest

DURATION_TOLERANCE = 0.10
MAX_REPLY_CHARS = 1200
_CAPTURE_ORDER_KEYS = {"capture_time", "chronological", "route", "time", "shot_order"}
_CAPTURE_BASES = {"capture_time", "route", "capture_order"}


def _fold(value: str) -> str:
    """NFC + Turkish-aware case fold ("KIRMIZI" == "Kırmızı"), whitespace collapsed."""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u0130", "i").replace("I", "i").replace("\u0131", "i")
    return " ".join(text.casefold().split())


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
class PlanFacts:
    """What a drafted plan verifiably contains. Missing facts stay None/empty."""

    clip_ids: tuple[str, ...] = ()
    title: str | None = None
    per_clip_text: dict[str, str] = field(default_factory=dict)
    inferred_text: dict[str, str] = field(default_factory=dict)
    positional_labels: tuple[str, ...] = ()
    duration_s: float | None = None
    ordering_basis: str | None = None
    ordering_fallback_clip_ids: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    # KRI-190: clips whose label is on a cut shorter than its reading time (the
    # clip itself is too short). A label the viewer cannot read is not "met".
    unreadable_label_clip_ids: tuple[str, ...] = ()
    # True when the facts come from an editor payload, which carries literal
    # on-screen text only (no per-clip structure, order or duration).
    editor: bool = False
    # The strategy's edit format, and how many video clips it renders (None when
    # the footage list was not available to count).
    edit_format: str | None = None
    video_clip_count: int | None = None
    # The item's opt-in Speech cleanup toggle (None = unknown): when on, a render
    # may cut pauses and retakes out of the take.
    speech_cleanup_enabled: bool | None = None
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
) -> PlanFacts:
    """Read verifiable facts off a serialized ``CreativeStrategy``.

    ``manifest`` is the turn's resolved creator manifest. Without it the beat,
    closing-shot and clip-count facts stay unknown (never guessed).
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
            if assignment.get("grounding") != "creator_text":
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
        **extra,
    )


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
    return PlanFacts(
        clip_ids=tuple(str(c) for c in record.get("clip_ids") or []),
        title=str(title) if title else None,
        per_clip_text=per_clip,
        inferred_text=inferred,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        ordering_basis=str(basis) if basis else None,
        ordering_fallback_clip_ids=tuple(
            str(c) for c in record.get("ordering_fallback_clip_ids") or []
        ),
        texts=tuple(text for text in (title, *per_clip.values()) if text),
        unreadable_label_clip_ids=tuple(str(c) for c in record.get("short_label_clip_ids") or []),
    )


def plan_facts_from_editor_payload(payload: Mapping[str, Any] | None) -> PlanFacts:
    """Editor drafts expose only literal on-screen text to the checkers."""
    if not payload:
        return PlanFacts()
    strings: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(json.dumps(payload, default=str)))
    return PlanFacts(texts=tuple(strings), editor=True)


def _receipt(
    req: BriefRequirement, status: str, reason: str | None, inferred: Iterable[str] = ()
) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=req.id,
        status=status,  # type: ignore[arg-type]
        reason=reason[:300] if reason else None,
        inferred=list(dict.fromkeys(str(x) for x in inferred))[:24],
    )


def _check_per_clip_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    wanted = _fold(req.literal or "")
    if facts.editor:
        # An editor payload has no per-clip structure: judge the literal if the
        # creator wrote one, otherwise say it can't be checked. Never "couldn't".
        if wanted and any(_contains_text(t, wanted) for t in facts.texts):
            return _receipt(req, "met", None)
        return _receipt(req, "partial", "I can't verify per-clip text on an editor edit.")
    ids = facts.clip_ids

    def text_for(index: int, clip: str) -> str | None:
        if clip in facts.per_clip_text:
            return facts.per_clip_text[clip]
        if index < len(facts.positional_labels):
            return facts.positional_labels[index]
        return None

    if req.scope.startswith("clip:"):
        clip = req.scope.split(":", 1)[1]
        index = ids.index(clip) if clip in ids else len(ids)
        value = text_for(index, clip)
        if value is None and clip not in ids and wanted:
            value = next((t for t in facts.texts if _contains_text(t, wanted)), None)
        if value is None:
            return _receipt(req, "not_possible", "That clip didn't get its own text in this draft.")
        if wanted and not _contains_text(value, wanted):
            return _receipt(req, "partial", "That clip's text isn't the exact text you gave.")
        guessed = [facts.inferred_text[clip]] if clip in facts.inferred_text else []
        return _receipt(req, "met", None, guessed)

    total = len(ids)
    if ids:
        count = sum(1 for i, clip in enumerate(ids) if text_for(i, clip) is not None)
    else:
        count = max(len(facts.per_clip_text), len(facts.positional_labels))
    inferred = [
        f"{facts.inferred_text[clip]}" for clip in ids if clip in facts.inferred_text
    ] or list(facts.inferred_text.values())
    if wanted and count:
        given = [*facts.per_clip_text.values(), *facts.positional_labels]
        if not any(_contains_text(t, wanted) for t in given):
            return _receipt(
                req, "partial", "The clips don't carry the exact text you gave.", inferred
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
            )
        return _receipt(req, "met", None, inferred)
    if count == 0:
        return _receipt(
            req,
            "not_possible",
            "No clip got its own text in this draft."
            if not total
            else f"None of the {total} clips got its own text in this draft.",
        )
    reason = f"Text landed on {count} of {total} clips." if total else f"Text on {count} clips."
    return _receipt(req, "partial", reason, inferred)


def _check_order(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    key = str(req.facts.get("key") or req.facts.get("by") or "").casefold()
    if not facts.ordering_basis:
        return _receipt(req, "partial", "I can't confirm the order this draft uses.")
    basis = facts.ordering_basis
    if key in _CAPTURE_ORDER_KEYS:
        if basis not in _CAPTURE_BASES:
            return _receipt(
                req,
                "partial",
                f"This draft is ordered by {basis.replace('_', ' ')}, not the order you asked for.",
            )
    elif not key or key != basis:
        # Nothing here can confirm an ordering this checker has no rule for.
        return _receipt(req, "partial", "I can't verify this ordering automatically.")
    if facts.ordering_fallback_clip_ids:
        n = len(facts.ordering_fallback_clip_ids)
        return _receipt(
            req,
            "partial",
            f"{n} clip{'s' if n != 1 else ''} had no capture time, so I kept "
            f"{'their' if n != 1 else 'its'} attachment order.",
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
# reply (like a requirement with no checker), never a failure notice.
_CANT_CHECK_BEATS = "I can't check the pop-ins on this draft yet."
_CANT_CHECK_TAKE = "I can't confirm this draft keeps your whole take."
_NEUTRAL_REASONS = frozenset({_CANT_CHECK_BEATS, _CANT_CHECK_TAKE})


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


def _check_timing(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if _wants_whole_take(req):
        return _check_whole_take(req, facts)
    target = req.facts.get("duration_s")
    if not isinstance(target, (int, float)) or target <= 0:
        return _receipt(req, "partial", "I can't verify this timing automatically.")
    if facts.duration_s is None:
        return _receipt(req, "partial", "I can't confirm this draft's length yet.")
    if abs(facts.duration_s - float(target)) <= DURATION_TOLERANCE * float(target):
        return _receipt(req, "met", None)
    return _receipt(
        req,
        "partial",
        f"This draft is about {facts.duration_s:g}s; you asked for {float(target):g}s.",
    )


def _check_literal_text(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    wanted = _fold(req.literal or "")
    if req.scope == "title" and facts.title:
        found = _fold(facts.title) == wanted
    else:
        found = any(_contains_text(t, wanted) for t in facts.texts)
    if found:
        return _receipt(req, "met", None)
    return _receipt(req, "partial", "That exact text isn't in this draft.")


def _has_checker(req: BriefRequirement) -> bool:
    """True when ``check_requirement`` can actually verify this requirement."""
    if req.kind == "text":
        return bool(req.scope == "per_clip" or req.scope.startswith("clip:") or req.literal)
    if req.kind == "timing":
        # "Fast but readable" has no number to check: that is "can't verify"
        # (neutral in the reply), not a failed requirement. "Keep my whole take"
        # is checkable against the edit format and clip count.
        return _has_duration_target(req) or _wants_whole_take(req)
    if req.kind in _BEAT_KINDS:
        return _wants_beats(req) or _wants_closing(req)
    return req.kind == "order"


def check_requirement(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt:
    if req.kind == "text":
        if req.scope == "per_clip" or req.scope.startswith("clip:"):
            return _check_per_clip_text(req, facts)
        if req.literal:
            return _check_literal_text(req, facts)
    elif req.kind == "order":
        return _check_order(req, facts)
    elif req.kind == "timing":
        return _check_timing(req, facts)
    elif req.kind in _BEAT_KINDS and (_wants_beats(req) or _wants_closing(req)):
        return _check_reaction_beats(req, facts)
    return _receipt(req, "partial", "I can't verify this one automatically yet.")


# KRI-190: requirement kinds the unified montage planner settles at render time. Its
# receipts are built from the plan it actually made, so a draft-time check of these is
# premature: the strategy draft has no per-clip text, order or timing yet, and would
# report "None of the 14 clips got its own text" for a video that then gets 14 labels.
UNIFIED_SETTLED_KINDS = frozenset({"text", "order", "timing"})


# Approval turns `audio_strategy` into the item's audio mode: only these two leave the
# voiceover lane, and the worker takes the unified planner only outside it.
_NON_VOICEOVER_AUDIO = frozenset({"original_audio", "licensed_music"})


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
    montage-family format, `montage_unified_plan_for`, and no voiceover lane. Approval
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
        and settings.montage_unified_plan_for(creator_id)
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


def build_receipts(
    requirements: Iterable[BriefRequirement], facts: PlanFacts
) -> list[RequirementReceipt]:
    return [check_requirement(req, facts) for req in requirements if req.live]


# ------------------------------------------------------------------------ reply

_LABEL = {"met": "Done", "partial": "Partly", "not_possible": "Couldn't"}


def reply_from_receipts(
    brief: CreativeBrief,
    receipts: list[RequirementReceipt],
    *,
    summary: str | None = None,
) -> str:
    """Compose the creator-facing reply from receipts only.

    The model's free-text summary is kept only when every requirement is met;
    otherwise the reply is exactly what was checked, so it cannot overclaim.
    """
    by_id = {req.id: req for req in brief.requirements}
    lines: list[str] = []
    guesses: list[str] = []
    for receipt in receipts:
        req = by_id.get(receipt.requirement_id)
        what = req.text() if req else receipt.requirement_id
        line = f"{_LABEL[receipt.status]}: {what}"
        if receipt.reason and receipt.status != "met":
            line += f" ({receipt.reason.rstrip('.')})"
        lines.append(line)
        guesses.extend(receipt.inferred)
    if guesses:
        shown = ", ".join(dict.fromkeys(guesses))
        lines.append(f"I guessed these, tell me if any is wrong: {shown}")
    checkable = {
        r.requirement_id
        for r in receipts
        if (req := by_id.get(r.requirement_id)) is not None
        and _has_checker(req)
        and r.reason not in _NEUTRAL_REASONS
    }
    # "Can't verify" is neutral: only a requirement a checker actually judged
    # can turn the reply into a failure notice.
    problem = any(r.status != "met" and r.requirement_id in checkable for r in receipts)
    head = ""
    if not problem and summary and summary.strip():
        head = summary.strip() + "\n"
    elif problem:
        head = "Not everything you asked for made it in:\n"
    text = head + "\n".join(f"- {line}" for line in lines)
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


__all__ = [
    "UNIFIED_SETTLED_KINDS",
    "BeatFact",
    "defers_to_unified_montage",
    "requirements_to_check_at_draft",
    "PlanFacts",
    "build_receipts",
    "check_requirement",
    "plan_facts_from_editor_payload",
    "plan_facts_from_strategy",
    "plan_facts_from_unified_montage",
    "reply_from_receipts",
]
