"""Conflict-choice questions (KRI-282): detect, ask, remember, follow.

When the creator's instructions cannot all be honoured at once (or can be honoured in
more than one reasonable way), the assistant must not silently pick one. It asks ONE
focused question with concrete options (``choice_question`` on the assistant question
event), the app answers with ``choice_selection`` (SubmitTurnBody), and the answer is
replayed from the thread's own events on every later turn, so it is followed and never
asked again. Same event-replay design as ``clip_selection``: no extra state.

Adding a conflict later = build a :class:`ConflictCandidate` (a deterministic detector
here, or one the planner flags) and give its ``conflict_id`` a consumer for the chosen
option key. The first case is ``order_vs_group``: "chronological" together with "group
by sport" when the clips were filmed mixed together.

KRI-476 (PR-C) adds ONE collector, :func:`collect_conflicts`, the only place that turns
an ambiguity into a question. It is route-independent (it reads the strategy, the brief
and the approved media snapshot, never a render route) and returns typed
:class:`UnresolvedChoice` items in a fixed priority order; :func:`resolve_choices`
folds the creator's earlier answers (scoped by ``input_digest``) into server-owned
strategy fields and returns at most the first still-open question.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.clip_facts import (
    CAPTURE_ORDER_KEYS,
    capture_from_assignment,
    order_by_capture_time,
)

CHOICE_QUESTION_VERSION = 1
MAX_CHOICE_OPTIONS = 6
_NAME_MAX = 40

CONFLICT_ORDER_VS_GROUP = "order_vs_group"
OPT_GROUP_FIRST = "group_first"
OPT_CHRONOLOGICAL = "chronological"
ORDER_VS_GROUP_OPTIONS = (OPT_GROUP_FIRST, OPT_CHRONOLOGICAL)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceSelectionIn(_Strict):
    """The app's answer to a ``choice_question`` (SubmitTurnBody.choice_selection)."""

    question_id: str = Field(min_length=1, max_length=64)
    option_key: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class ConflictOption:
    key: str
    label: str
    description: str | None = None
    recommended: bool = False
    # Short phrases a typed reply may use instead of the label ("extend it"). Matched
    # exactly after normalisation, never fuzzily (``match_open_choice``).
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictCandidate:
    """One detected (or planner-flagged) conflict, ready to be asked."""

    conflict_id: str
    reason: str
    intro: str
    options: tuple[ConflictOption, ...]
    # KRI-476: the collector's kind and the digest of the inputs the question depends on.
    # ``None`` keeps the legacy order-vs-group payload byte-identical.
    kind: str | None = None
    input_digest: str | None = None


# ── Detection: ordering vs grouping ───────────────────────────────────────────


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())[:_NAME_MAX]


def _join_names(names: Sequence[str]) -> str:
    shown = [_clean_name(n) for n in names if _clean_name(n)][:3]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def split_groups(
    ordered_ids: Sequence[str], groups: Sequence[tuple[str, Sequence[str]]]
) -> list[tuple[str, int]]:
    """``(name, stretches)`` for every group that is NOT one contiguous stretch.

    A clip in several groups is ambiguous and belongs to none; clips in no group are
    ignored (they never break a group's stretch). Same rule as the montage receipt, so
    "is there a conflict" and "did the render group them" cannot disagree.
    """
    position = {media_id: i for i, media_id in enumerate(ordered_ids)}
    tally: dict[str, int] = {}
    for _name, members in groups:
        for media_id in set(members):
            tally[media_id] = tally.get(media_id, 0) + 1
    owned = sorted(position[m] for m, n in tally.items() if n == 1 and m in position)
    split: list[tuple[str, int]] = []
    for name, members in groups:
        mine = {position[m] for m in set(members) if tally.get(m) == 1 and m in position}
        stretches, inside = 0, False
        for index in owned:
            if index in mine and not inside:
                stretches += 1
            inside = index in mine
        if stretches > 1:
            split.append((name, stretches))
    return split


def detect_order_vs_group(
    *,
    wants_capture_order: bool,
    groups: Sequence[tuple[str, Sequence[str]]],
    clips: Sequence[tuple[str, datetime | None]],
    noun: str = "group",
) -> ConflictCandidate | None:
    """Chronological order AND >=2 groups that the filming order interleaves.

    ``groups`` are the resolved group intents as ``(name, member media ids)``;
    ``clips`` are ``(media id, capture time)`` in attachment order. No conflict when the
    creator did not ask for filming order, when there is no capture time to order by
    (the montage then keeps attachment order and nothing is traded off), or when the
    clips already form one block per group in capture order.
    """
    if not wants_capture_order:
        return None
    known = {media_id for media_id, _t in clips}
    live = [(n, [m for m in members if m in known]) for n, members in groups]
    live = [(n, members) for n, members in live if n and members]
    if len(live) < 2:
        return None
    times = {media_id: moment for media_id, moment in clips if moment is not None}
    ordering = order_by_capture_time([media_id for media_id, _t in clips], times)
    if ordering.basis != "capture_time":
        return None
    if not split_groups(ordering.ordered_ids, live):
        return None
    names = _join_names([n for n, _m in live])
    word = noun if noun else "group"
    return ConflictCandidate(
        conflict_id=CONFLICT_ORDER_VS_GROUP,
        reason=f"your {names} clips were filmed mixed together, so I can't do both.",
        intro=f"You asked for a chronological video and for the clips grouped by {word}.",
        options=(
            ConflictOption(
                key=OPT_GROUP_FIRST,
                label=f"Group by {word}, chronological inside each {word}",
                description=(
                    f"Each {word} plays as one block; blocks start in the order they first "
                    "appear, and clips inside a block keep the order you filmed them."
                ),
                recommended=True,
            ),
            ConflictOption(
                key=OPT_CHRONOLOGICAL,
                label=f"Keep it strictly chronological; {word}s may interleave",
                description="Clips play in the exact order you filmed them.",
            ),
        ),
    )


# ── Question payload ──────────────────────────────────────────────────────────


def build_choice_question(
    candidate: ConflictCandidate, *, question_id: str | None = None
) -> dict[str, Any]:
    """The additive ``choice_question`` payload for the assistant question event."""
    options = []
    for option in candidate.options[:MAX_CHOICE_OPTIONS]:
        row: dict[str, Any] = {
            "key": option.key,
            "label": option.label,
            "recommended": bool(option.recommended),
        }
        if option.description:
            row["description"] = option.description
        if option.aliases:
            row["aliases"] = list(option.aliases)
        options.append(row)
    payload: dict[str, Any] = {
        "version": CHOICE_QUESTION_VERSION,
        "question_id": question_id or str(uuid.uuid4()),
        # Replay key: which conflict this answers. Old/other clients ignore it.
        "conflict": candidate.conflict_id,
        "options": options,
        "allow_free_text": True,
    }
    # Additive fields (clients ignore unknown keys): the collector kind and the digest
    # that scopes the answer to the exact inputs this question was asked about.
    if candidate.kind:
        payload["kind"] = candidate.kind
    if candidate.input_digest:
        payload["input_digest"] = candidate.input_digest
    return payload


def choice_question_text(candidate: ConflictCandidate) -> str:
    """Self-sufficient plain text (old builds show only this)."""
    options = candidate.options[:MAX_CHOICE_OPTIONS]
    if candidate.kind == CONFLICT_TITLE_TEXT and len(options) == 1:
        # The one option is a way out, not a limit: the typed words are the main path.
        return (
            f"{candidate.intro} Type the words you want and I'll use them exactly, "
            f'or reply "{options[0].label}".'
        )
    if len(options) == 1:
        # One way forward is a statement, not a choice.
        only = options[0]
        return (
            f"{candidate.intro} Unfortunately {candidate.reason} The most I can do is: "
            f'{only.label}. Reply "{only.label}" to go with that, or change your request.'
        )
    lines = [f"{candidate.intro} Unfortunately {candidate.reason} Which do you prefer?"]
    for index, option in enumerate(options, start=1):
        mark = " (recommended)" if option.recommended else ""
        lines.append(f"{index}. {option.label}{mark}")
    lines.append("Tap an option, or tell me in your own words.")
    return "\n".join(lines)


# ── Replaying thread events ───────────────────────────────────────────────────

Events = Iterable[tuple[str, "dict[str, Any] | None"]]


def _option_keys(question: Mapping[str, Any]) -> set[str]:
    return {
        str(o.get("key"))
        for o in question.get("options") or []
        if isinstance(o, Mapping) and o.get("key")
    }


# A recovery reply that restates an exhausted question in words keeps it answerable.
KEEP_OPEN_REASON = "unresolved_choice"

# Thread events can carry tags the loaders add (``tag_event``): the producing event type,
# and a user message's text. Untagged rows (older callers, tests) fall back to the payload.
EVENT_TYPE_KEY = "_event_type"
CONTENT_KEY = "_content"

# What each assistant-role event type does to an OPEN choice question. Only a conversational
# reply to a user turn taken AFTER the question may supersede it; everything asynchronous or
# non-conversational must leave it answerable (a render finishing or a memory write used to
# make a tap fail with "no longer open" and burned one of the two allowed asks). Verified
# against the producers in tasks/kria_runtime.py, kria/runtime.py, kria/drafts.py,
# routes/creation_threads.py, routes/creator_agent.py, services/creation_editor_actions.py,
# services/creator_memory_learning.py and tasks/creator_preparation.py.
CLOSES_AFTER_USER_REPLY = "closes_after_user_reply"
NEVER_CLOSES = "never_closes"
QUESTION_EVENT_EFFECT: dict[str, str] = {
    # The planner's / editor copilot's reply to a user turn: a text question, plan or
    # answer that supersedes the open question ("how many clips?" must not turn the next
    # "2" into this question's answer).
    "assistant_response": CLOSES_AFTER_USER_REPLY,
    # A reply that asks something else (voiceover required, dispatch unavailable).
    "assistant_question": CLOSES_AFTER_USER_REPLY,
    # The turn failed; the user's reply was not processed, so the question is still the
    # live ask.
    "assistant_error": NEVER_CLOSES,
    # Asynchronous: an earlier render failed, finished, or was reviewed. Not a reply.
    "assistant_render_failed": NEVER_CLOSES,
    "generation_ready": NEVER_CLOSES,
    "assistant_review": NEVER_CLOSES,
    # Outbox / background workers (creator memory) and thread bookkeeping.
    "memory_updated": NEVER_CLOSES,
    "status_update": NEVER_CLOSES,
    # Format / media prerequisite prompts raised by thread actions, not by a reply.
    "format_prompt": NEVER_CLOSES,
    "media_prompt": NEVER_CLOSES,
    # Draft bookkeeping written by the draft tools themselves.
    "draft_applied": NEVER_CLOSES,
    "draft_undone": NEVER_CLOSES,
}


def tag_event(
    role: str, payload: Mapping[str, Any] | None, event_type: str | None, content: str | None
) -> tuple[str, dict[str, Any]]:
    """``(role, payload)`` plus the event type and (for a user message) its text."""

    tagged: dict[str, Any] = dict(payload or {})
    if event_type:
        tagged[EVENT_TYPE_KEY] = event_type
    if role == "user" and content:
        tagged[CONTENT_KEY] = " ".join(str(content).split())[:400]
    return role, tagged


def _closes_open_question(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get(EVENT_TYPE_KEY)
    if event_type is None:
        # Untagged: only something that looks like a conversational reply may close.
        event_type = "assistant_response" if "turn_value" in payload else None
    return QUESTION_EVENT_EFFECT.get(str(event_type)) == CLOSES_AFTER_USER_REPLY


def _keeps_question_open(payload: Mapping[str, Any]) -> bool:
    coverage = payload.get("brief_coverage")
    return isinstance(coverage, Mapping) and coverage.get("reason") == KEEP_OPEN_REASON


def latest_open_choice_question(events: Events) -> dict[str, Any] | None:
    """The choice question a plain reply or a tap may still answer, else ``None``.

    ``events`` are ``(role, payload)`` in chronological order. A question stays open
    until, and only until:

    * a ``choice_selection`` answers it;
    * a newer ``choice_question`` replaces it;
    * the assistant replies to a user turn taken AFTER it with a text question / plan
      (``QUESTION_EVENT_EFFECT``; asynchronous events never close it), except a recovery
      that restates it in words (``brief_coverage.reason == "unresolved_choice"``);
    * it has been asked ``MAX_ASKS_PER_QUESTION`` times and a user message that is not an
      answer follows.
    """
    last: dict[str, Any] | None = None
    is_open = False
    user_since = False
    asked: dict[tuple[Any, Any], int] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant":
            question = payload.get("choice_question")
            if isinstance(question, dict):
                key = (question.get("conflict"), question.get("input_digest"))
                asked[key] = asked.get(key, 0) + 1
                last, is_open, user_since = question, True, False
            elif last is not None and _keeps_question_open(payload):
                is_open = True
            elif user_since and _closes_open_question(payload):
                is_open = False
        elif role == "user":
            selection = payload.get("choice_selection")
            if isinstance(selection, dict):
                if last is not None and selection.get("question_id") == last.get("question_id"):
                    last, is_open = None, False
            elif last is not None and is_open:
                user_since = True
                key = (last.get("conflict"), last.get("input_digest"))
                if asked.get(key, 0) >= MAX_ASKS_PER_QUESTION:
                    is_open = False
    return last if is_open else None


def fold_choice_answers(events: Events) -> dict[str, str]:
    """``conflict_id -> chosen option key`` replayed from the thread (last answer wins).

    Only a selection that answers a question the server actually asked, with an option
    that question offered, counts.
    """
    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, str] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            question = payload["choice_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("choice_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        conflict = question.get("conflict") if question else None
        if not conflict or selection.get("option_key") not in _option_keys(question):
            continue
        answers[str(conflict)] = str(selection["option_key"])
    return answers


# ── The collector (KRI-476 / PR-C) ────────────────────────────────────────────

CONFLICT_DURATION_VS_COUNT = "duration_vs_count"
CONFLICT_ORDER_BASIS = "order_basis"
CONFLICT_TEXT_PLACEMENT = "text_placement"
CONFLICT_TITLE_TEXT = "title_text"
# KRI-479 (voice behind footage; only for a `voice_mode == "continuous"` plan).
CONFLICT_VOICE_VS_DURATION = "voice_vs_duration"
CONFLICT_WHICH_VOICE = "which_voice"
# Fixed priority: an answer can change a later detector's inputs, so exactly one question
# is asked per turn, in this order.
CONFLICT_PRIORITY = (
    CONFLICT_ORDER_BASIS,
    CONFLICT_WHICH_VOICE,
    CONFLICT_TEXT_PLACEMENT,
    CONFLICT_TITLE_TEXT,
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_VOICE_VS_DURATION,
)

OPT_EXTEND = "extend"
OPT_FEWER = "fewer"
OPT_ATTACHMENT_ORDER = "attachment_order"
OPT_UNORDERED = "unordered"
OPT_OWN_SEQUENCE = "own_sequence"
OPT_NO_TITLE = "no_title"
OPT_MATCH_VOICE = "match_voice"
OPT_SILENT_TAIL = "silent_tail"
OPT_LENGTH_30 = "length_30"
OPT_LENGTH_60 = "length_60"

# What an "order I filmed them" requirement means to the contract (brief order keys).
ATTACHMENT_ORDER_KEY = "attachment"

# The most clips a draft names; a hard bound on the evenly-spaced subset.
_MAX_FIT_FRAMES_FPS = 30
# A choice is asked at most this many times for the same inputs: the question and ONE
# re-ask. After that the creator's plan goes through unchanged (never a default for them).
MAX_ASKS_PER_QUESTION = 2


@dataclass(frozen=True)
class ChoiceCapability:
    """What the pipeline can execute, so a question only offers what will happen."""

    # Readable-shot floor; ``None`` = the montage planner's own ``MIN_READABLE_SHOT_S``.
    min_readable_shot_s: float | None = None
    max_duration_s: float = 120.0
    # Whether a creator-typed clip sequence can be received and executed. It cannot
    # today (the tray order is set in the editor, never inside a question).
    creator_sequence_supported: bool = False
    # Whose draft this is. ``title_text`` needs it to know whether the unified phone
    # montage will render the draft (its title receipt is only judged at render time);
    # ``None`` = unknown, so that question is never asked.
    creator_id: object = None


@dataclass(frozen=True)
class UnresolvedChoice:
    """One material, askable ambiguity and everything needed to ask and follow it."""

    kind: str
    conflict_id: str
    field_path: str
    requirement_ids: tuple[str, ...]
    intro: str
    reason: str
    options: tuple[ConflictOption, ...]
    input_digest: str
    # option key -> {"strategy": {...}} rewrites applied when that option is chosen.
    effects: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def candidate(self) -> ConflictCandidate:
        return ConflictCandidate(
            conflict_id=self.conflict_id,
            reason=self.reason,
            intro=self.intro,
            options=self.options,
            kind=self.kind,
            input_digest=self.input_digest,
        )

    def recommended(self) -> ConflictOption:
        return next((o for o in self.options if o.recommended), self.options[0])


def _as_dict(strategy: Any) -> dict[str, Any]:
    if strategy is None:
        return {}
    if isinstance(strategy, Mapping):
        return dict(strategy)
    dump = getattr(strategy, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json", exclude_none=True))
    return {}


def _digest(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _norm_text(value: object) -> str:
    # Same normalisation as the render contract's text matching (NFC, one space).
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _live(brief: Any) -> list[Any]:
    live = getattr(brief, "live", None)
    return list(live()) if callable(live) else []


def _rows(snapshot: Mapping[str, Any] | None) -> list[Any]:
    return list((snapshot or {}).get("clip_assignments") or [])


def _requested_durations(brief: Any) -> tuple[list[float], list[str], str]:
    """The creator's own lengths: LIVE brief timing requirements only.

    A strategy ``target_duration_s`` is NOT evidence: the Creator model must always emit
    one (it picks a length from the footage when the creator named none), and the server
    marks it ``target_duration_requested`` either way.
    """

    durations: list[float] = []
    requirement_ids: list[str] = []
    quoted = ""
    for req in _live(brief):
        if req.kind != "timing":
            continue
        value = _number((req.facts or {}).get("duration_s"))
        if value is not None:
            durations.append(value)
            requirement_ids.append(str(req.id))
            quoted = quoted or " ".join(str(req.description or "").split())[:80]
    return durations, requirement_ids, quoted


def _evenly_spaced(ids: Sequence[str], count: int) -> list[str]:
    total = len(ids)
    if count >= total:
        return list(ids)
    if count <= 1:
        return list(ids[:1])
    picks = [round(i * (total - 1) / (count - 1)) for i in range(count)]
    return [ids[i] for i in picks]


def _clean_seconds(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 1)


def _montage_exempt(strategy: Mapping[str, Any]) -> bool:
    """Strategies whose length is not a promise about N flash-cut clips.

    Only a plain montage cuts every clip to ``target_duration_s``. Voiceover, creator-song
    and lip-sync montages are timed by the recording / song, speech and day-vlog timelines
    by their own material (the voiceover and lip-sync planners never read the strategy
    length), and a cadence or exact photo/video timing is the creator's own cut length.
    """

    return bool(
        strategy.get("edit_format", "montage") != "montage"
        or strategy.get("montage_cadence")
        or strategy.get("mixed_media_timing")
        # A continuous voice (KRI-479) is composed by the voice-behind-footage composer, which
        # cuts the picture clips to the length: unlike a camera-audio excerpt montage, the
        # clips-per-length question applies to it.
        or (strategy.get("montage_audio") and _continuous_voice(strategy) is None)
        or strategy.get("archetype")
        or strategy.get("execution_contract")
        or strategy.get("audio_strategy") in ("voiceover", "user_song")
        or strategy.get("song_sync")
    )


def _included_ids(strategy: Mapping[str, Any]) -> set[str] | None:
    """Clips the creator's resolved ``include`` intents keep (None = no such intent)."""

    kept: set[str] = set()
    found = False
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "include":
            continue
        if intent.get("status", "resolved") != "resolved":
            continue
        found = True
        kept |= {
            str(a["media_id"])
            for a in intent.get("assignments") or []
            if isinstance(a, Mapping) and a.get("media_id")
        }
    return kept if found else None


def _duration_vs_count(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    if _montage_exempt(strategy):
        return None
    durations, requirement_ids, quoted = _requested_durations(brief)
    if not durations:
        # Only a length the CREATOR stated is a promise. The model's own pick (and the 24 s
        # default) never asks, however many clips there are.
        return None
    if any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        return None  # conflicting lengths are the contract's own typed error
    if any(req.kind == "select" for req in _live(brief)) and _included_ids(strategy) is None:
        return None  # the creator chose a subset we cannot count: never guess N
    seconds = durations[0]
    ids = [str(r["media_id"]) for r in rows if isinstance(r, Mapping) and r.get("media_id")]
    if not ids or len(set(ids)) != len(ids):
        return None
    kept = _included_ids(strategy)
    voice_id = _continuous_voice(strategy)
    clip_ids = [m for m in ids if (kept is None or m in kept) and m != voice_id]
    # The selection the draft carries IS the clip set (like `_order_basis`): 8 selected
    # clips of 42 are not 42, and `fewer` must never re-add a clip that was left out.
    chosen = {str(m) for m in strategy.get("selected_media_ids") or []}
    if chosen:
        clip_ids = [m for m in clip_ids if m in chosen]
    if not clip_ids:
        return None
    floor_s = cap.min_readable_shot_s
    if floor_s is None:
        from app.pipeline.unified_montage import MIN_READABLE_SHOT_S  # noqa: PLC0415

        floor_s = MIN_READABLE_SHOT_S
    floor_frames = max(1, round(floor_s * _MAX_FIT_FRAMES_FPS))
    count = len(clip_ids)
    if count * floor_frames <= round(seconds * _MAX_FIT_FRAMES_FPS):
        return None
    needed = math.ceil(count * floor_frames / _MAX_FIT_FRAMES_FPS * 10 - 1e-9) / 10
    can_extend = needed <= cap.max_duration_s
    fit = max(1, int(seconds * _MAX_FIT_FRAMES_FPS) // floor_frames)
    subset = _evenly_spaced(clip_ids, fit)
    shown = _clean_seconds(needed)
    asked = _clean_seconds(seconds)
    said = quoted or f"{asked} seconds"
    options = [
        ConflictOption(
            key=OPT_FEWER,
            label=f"Keep {asked} seconds with {len(subset)} clips",
            description=(
                f"Uses {len(subset)} of your {count} clips, spread evenly through your "
                "footage; the others are left out."
            ),
            recommended=not can_extend,
            aliases=("fewer", "fewer clips", "use fewer", "use fewer clips", "keep it short"),
        )
    ]
    effects: dict[str, Mapping[str, Any]] = {
        OPT_FEWER: {
            "strategy": {
                # The continuous voice's clip stays selected: it is heard, never counted.
                "selected_media_ids": [*subset, voice_id] if voice_id else subset,
                "media_scope": "selected",
                "target_duration_s": _clean_seconds(seconds),
                "target_duration_requested": True,
            }
        }
    }
    if can_extend:
        options.insert(
            0,
            ConflictOption(
                key=OPT_EXTEND,
                label=f"Extend it to {shown} seconds",
                description=(
                    f"Every clip gets at least {floor_s:g} seconds, so all {count} stay in."
                ),
                recommended=True,
                aliases=("extend", "extend it", "longer", "make it longer", "extend the length"),
            ),
        )
        effects[OPT_EXTEND] = {
            "strategy": {
                "target_duration_s": _clean_seconds(needed),
                "target_duration_requested": True,
            }
        }
    return UnresolvedChoice(
        kind=CONFLICT_DURATION_VS_COUNT,
        conflict_id=CONFLICT_DURATION_VS_COUNT,
        field_path="target_duration_s",
        requirement_ids=tuple(requirement_ids),
        intro=f'Your brief says "{said}" and this edit uses {count} clips.',
        reason=(
            f"{count} clips can't each stay on screen long enough to be seen in {asked} "
            f"seconds (each needs about {floor_s:g} seconds)."
        ),
        options=tuple(options),
        # The creator's inputs only: the model's own selection or length never reopens it.
        input_digest=_digest(CONFLICT_DURATION_VS_COUNT, sorted(ids), sorted(clip_ids), seconds),
        effects=effects,
    )


_ATTACHMENT_ALIASES = (
    "upload order",
    "order i added them",
    "the order i added them",
    "order i uploaded them",
    "attachment order",
    "as uploaded",
    "as added",
)


def _placed_sequence(strategy: Mapping[str, Any]) -> bool:
    """Did the server already place the creator's described sequence ("start with X")?"""

    return any(
        isinstance(i, Mapping)
        and i.get("op") == "order"
        and i.get("status", "resolved") == "resolved"
        and i.get("assignments")
        for i in strategy.get("resolved_clip_intents") or []
    )


def _order_basis(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    capture_ids: list[str] = []
    rule_ids: list[str] = []
    rule_text: list[str] = []
    for req in _live(brief):
        if req.kind != "order":
            continue
        key = str((req.facts or {}).get("key") or "").casefold()
        if key in CAPTURE_ORDER_KEYS:
            capture_ids.append(str(req.id))
        elif key != ATTACHMENT_ORDER_KEY:
            # A key-less or non-capture rule ("clips 1, 2, 3 in that sequence", "along my
            # route") the contract cannot verify from the approved media.
            rule_ids.append(str(req.id))
            rule_text.append(_norm_text(req.description or ""))
    wants_capture = strategy.get("ordering_choice") == "chronological" or bool(capture_ids)
    selected = {str(m) for m in strategy.get("selected_media_ids") or []}
    if selected:
        rows = [r for r in rows if isinstance(r, Mapping) and r.get("media_id") in selected]
    # Exactly the contract's own "can I resolve the order" shape, so a capture question is
    # asked only for the one thing a creator can fix: clips with no capture time.
    valid = bool(rows) and all(isinstance(r, Mapping) and r.get("media_id") for r in rows)
    ids = [str(r["media_id"]) for r in rows] if valid else []
    if valid and (len(set(ids)) != len(ids) or (selected and set(ids) != selected)):
        valid, ids = False, []
    missing = []
    if valid and wants_capture:
        for row in rows:
            capture = capture_from_assignment(row)
            if not (capture and capture.capture_time):
                missing.append(str(row["media_id"]))
    ask_capture = bool(missing)
    ask_rule = bool(rule_ids) and not _placed_sequence(strategy)
    if not (ask_capture or ask_rule):
        return None
    if ask_capture:
        many = len(ids)
        some = (
            "none of your clips have a filming time"
            if len(missing) == many
            else f"{len(missing)} of your {many} clips have no filming time"
        )
        filmed = strategy.get("ordering_choice") == "chronological" or any(
            str((req.facts or {}).get("key") or "").casefold() in ("capture_time", "chronological")
            for req in _live(brief)
            if req.kind == "order"
        )
        intro = (
            "You asked for the clips in the order you filmed them."
            if filmed
            else "You asked for the clips in a specific order that follows when they were filmed."
        )
        reason = f"{some}, so I can't put them in that order."
        unordered_label = "Continue without a fixed order"
        unordered_text = (
            "I won't promise or check a particular order; the clips still play in a "
            "sensible sequence."
        )
        covered = capture_ids + (rule_ids if ask_rule else [])
    else:
        intro = "You gave me a specific order for the clips."
        reason = "I can't check that order against your clips, so I can't promise it."
        unordered_label = "Continue without a fixed order"
        unordered_text = (
            "I won't promise or check a particular order; the clips still play in a "
            "sensible sequence."
        )
        covered = rule_ids
    options = [
        ConflictOption(
            key=OPT_ATTACHMENT_ORDER,
            label="Use the order you added the clips",
            description="The clips play in the order they were added to this project.",
            recommended=True,
            aliases=_ATTACHMENT_ALIASES,
        )
    ]
    if cap.creator_sequence_supported:
        options.append(
            ConflictOption(
                key=OPT_OWN_SEQUENCE,
                label="Use the order I give you",
                description="You tell me the order of the clips.",
                aliases=("my own order", "my own sequence"),
            )
        )
    options.append(
        ConflictOption(
            key=OPT_UNORDERED,
            label=unordered_label,
            description=unordered_text,
            aliases=(
                "without order",
                "no order",
                "skip the order",
                "continue without order",
                "no fixed order",
            ),
        )
    )
    return UnresolvedChoice(
        kind=CONFLICT_ORDER_BASIS,
        conflict_id=CONFLICT_ORDER_BASIS,
        field_path="ordering_choice",
        requirement_ids=tuple(covered),
        intro=intro,
        reason=reason,
        options=tuple(options),
        input_digest=_digest(
            CONFLICT_ORDER_BASIS,
            sorted(ids),
            sorted(missing),
            sorted(rule_text) if ask_rule else [],
        ),
    )


def _text_placement(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> list[UnresolvedChoice]:
    labels = [str(t) for t in strategy.get("shot_labels") or []]
    found: list[UnresolvedChoice] = []
    for req in _live(brief):
        if not getattr(req, "is_shot_text", False):
            continue
        matching = [
            i for i, text in enumerate(labels) if _norm_text(text) == _norm_text(req.literal)
        ]
        if len(matching) < 2:
            # One match is placed; none is a planner miss (a repair, never a question).
            continue
        picked = matching[:MAX_CHOICE_OPTIONS]
        options = tuple(
            ConflictOption(
                key=f"shot_{i + 1}",
                label=f"On shot {i + 1}",
                description=f"This line belongs on shot {i + 1} of the draft.",
                recommended=n == 0,
                aliases=(f"shot {i + 1}", f"shot number {i + 1}"),
            )
            for n, i in enumerate(picked)
        )
        found.append(
            UnresolvedChoice(
                kind=CONFLICT_TEXT_PLACEMENT,
                conflict_id=f"{CONFLICT_TEXT_PLACEMENT}:{req.id}",
                field_path="shot_labels[]",
                requirement_ids=(str(req.id),),
                intro=f'You gave me the words "{_clean_name(str(req.literal))}" for one shot.',
                reason=(
                    "those words appear on "
                    f"{_join_names([f'shot {i + 1}' for i in matching])} of the draft, "
                    "so I can't tell which shot you mean."
                ),
                options=options,
                input_digest=_digest(
                    CONFLICT_TEXT_PLACEMENT, str(req.id), _norm_text(req.literal), matching
                ),
            )
        )
    return found


# Exact (after `normalize_reply`) replies that decline the title: never a substring match, so
# real words ("No Plans", "none of us slept") are never taken for an answer.
_NO_TITLE_ALIASES = (
    "no",
    "none",
    "nope",
    "no thanks",
    "skip",
    "skip it",
    "skip title",
    "skip the title",
    "no title",
    "no title please",
    "no title thanks",
    "no hook",
    "without title",
    "without a title",
    "continue without title",
    "dont add a title",
    "don t add a title",
    "do not add a title",
    "leave it off",
    "leave the title off",
    "başlık olmasın",
    "başlıksız",
    "başlık yok",
)


def _title_text(
    strategy: Mapping[str, Any],
    brief: Any,
    rows: list[Any],
    cap: ChoiceCapability,
    *,
    clip_paths: Sequence[Any] = (),
) -> UnresolvedChoice | None:
    """A title was asked for with no words and nothing the renderer may title with.

    The unified phone montage settles a ``text`` requirement at render time and then
    BLOCKS the render when the title receipt is not met (a title is never a default, a
    model-written hook or unrequested place text), so the draft-time receipt check skips
    it (``UNIFIED_SETTLED_KINDS``) and the creator only learns after approving. Asked
    here instead, before approval. Whether a title source exists is decided by the
    renderer's own ``title_source_exists`` so the question and the render cannot disagree.
    """

    if cap.creator_id is None:
        return None
    from app.kria.brief_checks import defers_to_unified_montage  # noqa: PLC0415
    from app.pipeline.unified_montage import title_source_exists  # noqa: PLC0415

    wanted = [
        req
        for req in _live(brief)
        if req.kind == "text" and req.scope == "title" and not str(req.literal or "").strip()
    ]
    if not wanted:
        return None
    audio = strategy.get("montage_audio")
    if (
        isinstance(audio, Mapping)
        and audio.get("preserve_source_audio")
        and audio.get("source_media_ids")
    ):
        # `contract.audio_source_ids` takes the speech-excerpt lane
        # (`run_phone_speech_montage_job`): it has no title handling and never blocks on one.
        return None
    if not defers_to_unified_montage(
        creator_id=cap.creator_id,
        edit_format=strategy.get("edit_format") or "montage",
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    ):
        return None  # the draft-time receipts judge this format themselves
    if title_source_exists(strategy, brief):
        return None
    return title_text_choice(str(req.id) for req in wanted)


def title_text_choice(requirement_ids: Iterable[str]) -> UnresolvedChoice:
    """The ``title_text`` question for these wordless title requirements.

    Shared by the draft-time detector and the observer that re-opens the same question
    after the render-time block, so both carry the same ``conflict_id`` / ``input_digest``
    and an answer given to either is the answer the gate replays.
    """

    ids = sorted({str(i) for i in requirement_ids})
    return UnresolvedChoice(
        kind=CONFLICT_TITLE_TEXT,
        conflict_id=CONFLICT_TITLE_TEXT,
        field_path="opening_title",
        requirement_ids=tuple(ids),
        intro=(
            "You asked for a title on the opening, but you didn't tell me the words, and I "
            "don't write on-screen text for you."
        ),
        reason="I can't add a title without them.",
        options=(
            ConflictOption(
                key=OPT_NO_TITLE,
                label="Continue without a title",
                description="The video is made without an opening title.",
                # Never recommended: a delegation ("you decide") must not drop the title the
                # creator asked for.
                recommended=False,
                aliases=_NO_TITLE_ALIASES,
            ),
        ),
        input_digest=_digest(CONFLICT_TITLE_TEXT, ids),
    )


# ── Continuous voice (KRI-479) ────────────────────────────────────────────────

# A voice longer than this with no length asked for is a question ("how long?"); at or
# under it the edit simply follows the voice (the short-form ceiling).
_VOICE_IMPLICIT_ASK_S = 60.0
# A voice shorter than the asked length by more than this is a question.
_VOICE_SHORT_BY_S = 1.0
_MIN_ASKED_VOICE_S = 3.0  # ProposalDuration floor


def _voice_audio(strategy: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``montage_audio`` of a ``voice_mode == "continuous"`` strategy, else None."""

    if strategy.get("voice_mode") != "continuous":
        return None
    audio = strategy.get("montage_audio")
    if not isinstance(audio, Mapping) or not audio.get("preserve_source_audio"):
        return None
    return audio


def _continuous_voice(strategy: Mapping[str, Any]) -> str | None:
    """The ONE named voice clip of a continuous-voice plan, else None."""

    audio = _voice_audio(strategy)
    ids = [str(m) for m in (audio or {}).get("source_media_ids") or [] if m]
    return ids[0] if len(ids) == 1 else None


def _row_duration_s(row: Any) -> float | None:
    """Source length of one snapshot row (explicit, else the phone receipt's original)."""

    if not isinstance(row, Mapping):
        return None
    contract = row.get("upload_contract")
    proxy = contract.get("proxy") if isinstance(contract, Mapping) else None
    original = proxy.get("original") if isinstance(proxy, Mapping) else None
    for candidate in (
        row.get("duration_s"),
        original.get("duration_s") if isinstance(original, Mapping) else None,
        proxy.get("duration_s") if isinstance(proxy, Mapping) else None,
    ):
        value = _number(candidate)
        if value is not None:
            return value
    return None


def _spoken_quote(row: Mapping[str, Any]) -> str:
    """The first few words of a clip's speech: enough to recognise the take, no more."""

    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "video") == "image" else "video"
    speech = clip_record(row.get("analysis"), kind=kind).speech
    words = " ".join(str(speech.transcript or "").split()).split(" ")
    quote = " ".join(w for w in words[:8] if w)
    return quote[:60]


def _has_speech(row: Mapping[str, Any]) -> bool:
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "video") == "image" else "video"
    return bool(clip_record(row.get("analysis"), kind=kind).speech.has_speech)


def _which_voice(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    audio = _voice_audio(strategy)
    if audio is None:
        return None
    named = [str(m) for m in audio.get("source_media_ids") or [] if m]
    if len(named) == 1:
        return None
    by_id = {str(r["media_id"]): r for r in rows if isinstance(r, Mapping) and r.get("media_id")}
    pool = (
        [m for m in named if m in by_id]
        if named
        else [m for m, r in by_id.items() if r.get("kind", "video") != "image" and _has_speech(r)]
    )
    if len(pool) < 2:
        return None  # one speaker is not a question, and nothing is guessed either
    pool = pool[:MAX_CHOICE_OPTIONS]
    options: list[ConflictOption] = []
    effects: dict[str, Mapping[str, Any]] = {}
    for index, media_id in enumerate(pool, start=1):
        row = by_id[media_id]
        length = _row_duration_s(row)
        quote = _spoken_quote(row)
        label = f"Clip {index}" + (f" ({_clean_seconds(length)} s)" if length else "")
        label += f": \u201c{quote}\u201d" if quote else ""
        key = f"clip_{index}"
        options.append(
            ConflictOption(
                key=key,
                label=label,
                description="Use this recording's voice under the other clips.",
                aliases=(f"clip {index}",),
            )
        )
        effects[key] = {
            "strategy": {
                "montage_audio": {
                    **{k: v for k, v in audio.items() if k != "source_media_ids"},
                    "source_media_ids": [media_id],
                }
            }
        }
    return UnresolvedChoice(
        kind=CONFLICT_WHICH_VOICE,
        conflict_id=CONFLICT_WHICH_VOICE,
        field_path="montage_audio.source_media_ids[]",
        requirement_ids=(),
        intro="More than one of your clips has someone talking.",
        reason="Only one recording can be the voice that plays under the rest.",
        options=tuple(options),
        input_digest=_digest(CONFLICT_WHICH_VOICE, sorted(pool)),
        effects=effects,
    )


def _voice_vs_duration(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    voice_id = _continuous_voice(strategy)
    if voice_id is None:
        return None
    row = next((r for r in rows if isinstance(r, Mapping) and r.get("media_id") == voice_id), None)
    length = _row_duration_s(row)
    if length is None or length < _MIN_ASKED_VOICE_S:
        return None
    durations, requirement_ids, quoted = _requested_durations(brief)
    if any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        return None  # conflicting lengths are the contract's own typed error
    if durations:
        asked = durations[0]
        # A longer voice is trimmed to the asked length and disclosed: never a question.
        if length >= asked - _VOICE_SHORT_BY_S:
            return None
        whole = int(math.floor(length))
        if whole < _MIN_ASKED_VOICE_S:
            return None
        asked_s, voice_s = _clean_seconds(asked), _clean_seconds(whole)
        said = quoted or f"{asked_s} seconds"
        options = (
            ConflictOption(
                key=OPT_MATCH_VOICE,
                label=f"End the edit when your voice ends ({voice_s} seconds)",
                description="The video is exactly as long as your voice.",
                recommended=True,
                aliases=("end when my voice ends", "match my voice", "match the voice"),
            ),
            ConflictOption(
                key=OPT_SILENT_TAIL,
                label=(
                    f"Keep {asked_s} seconds, the last {_clean_seconds(asked - whole)} "
                    "seconds play without voice"
                ),
                description="Your voice plays first, then the footage carries on in silence.",
                aliases=("keep the length", "keep the length without voice"),
            ),
        )
        effects: dict[str, Mapping[str, Any]] = {
            OPT_MATCH_VOICE: {
                "strategy": {"target_duration_s": voice_s, "target_duration_requested": True}
            },
            OPT_SILENT_TAIL: {},
        }
        intro = f'Your brief says "{said}" and your voice clip runs {voice_s} seconds.'
        reason = "Your voice can't fill the whole video."
    elif length > _VOICE_IMPLICIT_ASK_S:
        options = (
            ConflictOption(
                key=OPT_LENGTH_30,
                label="30 seconds",
                description="The first part of your voice, as a quick video.",
                recommended=True,
                aliases=("30", "30 seconds", "thirty seconds"),
            ),
            ConflictOption(
                key=OPT_LENGTH_60,
                label="60 seconds",
                description="A minute of your voice.",
                aliases=("60", "60 seconds", "a minute", "one minute"),
            ),
        )
        effects = {
            OPT_LENGTH_30: {
                "strategy": {"target_duration_s": 30, "target_duration_requested": True}
            },
            OPT_LENGTH_60: {
                "strategy": {"target_duration_s": 60, "target_duration_requested": True}
            },
        }
        intro = f"Your voice clip runs {_clean_seconds(length)} seconds."
        reason = "How long should the video be?"
        requirement_ids = []
    else:
        return None
    return UnresolvedChoice(
        kind=CONFLICT_VOICE_VS_DURATION,
        conflict_id=CONFLICT_VOICE_VS_DURATION,
        field_path="target_duration_s",
        requirement_ids=tuple(requirement_ids),
        intro=intro,
        reason=reason,
        options=options,
        input_digest=_digest(
            CONFLICT_VOICE_VS_DURATION,
            voice_id,
            round(length * 1000),
            durations[0] if durations else None,
        ),
        effects=effects,
    )


def collect_conflicts(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    capability: ChoiceCapability | None = None,
) -> list[UnresolvedChoice]:
    """Every askable, route-independent ambiguity, in fixed priority order.

    The only place that turns an ambiguity into a question. It reads the strategy, the
    brief and the SAME approved media snapshot approval later binds; it never reads chat
    text or a render route. A conflict is returned only when the creator can resolve it
    with one of the options listed; an unsupported combination is not a question (it is a
    typed decline), and neither is a technical failure.
    """

    data = _as_dict(strategy)
    cap = capability or ChoiceCapability()
    rows = _rows(media_snapshot)
    found: list[UnresolvedChoice] = []
    clip_paths = list((media_snapshot or {}).get("clip_paths") or [])
    for detector in (
        _order_basis,
        _text_placement,
        functools.partial(_title_text, clip_paths=clip_paths),
        _duration_vs_count,
        _which_voice,
        _voice_vs_duration,
    ):
        result = detector(data, brief, rows, cap)
        if isinstance(result, list):
            found.extend(result)
        elif result is not None:
            found.append(result)
    rank = {kind: i for i, kind in enumerate(CONFLICT_PRIORITY)}
    return sorted(found, key=lambda c: rank.get(c.kind, len(rank)))  # stable within a kind


def answered_conflict_ids(strategy: Any) -> set[str]:
    return {
        str(a.get("conflict"))
        for a in _as_dict(strategy).get("choice_answers") or []
        if isinstance(a, Mapping)
    }


def open_conflicts(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    capability: ChoiceCapability | None = None,
) -> list[UnresolvedChoice]:
    """Conflicts the strategy has NOT recorded an answer for (the approval backstop).

    Answers are attached only by :func:`resolve_choices` after its digest check, so a
    conflict id present in ``choice_answers`` counts as answered here.
    """

    answered = answered_conflict_ids(strategy)
    return [
        c
        for c in collect_conflicts(strategy, brief, media_snapshot, capability)
        if c.conflict_id not in answered
    ]


# ── Answers: scoped replay, free text, folding ────────────────────────────────


@dataclass(frozen=True)
class ScopedAnswer:
    option_key: str
    input_digest: str | None
    # The creator handed the decision to us ("you choose"): the recommended option,
    # recorded as such and disclosed. Never set for an unanswered question.
    delegated: bool = False
    # Position of the answering event in the history (later non-answer messages follow it).
    index: int = -1


def _event_list(events: Events) -> list[tuple[str, Any]]:
    return [(role, payload) for role, payload in events]


def fold_scoped_answers(events: Events) -> dict[str, ScopedAnswer]:
    """``conflict_id -> (option, input_digest of the question it answered)``, last wins."""

    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, ScopedAnswer] = {}
    for position, (role, payload) in enumerate(_event_list(events)):
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            question = payload["choice_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("choice_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        conflict = question.get("conflict") if question else None
        if not conflict or selection.get("option_key") not in _option_keys(question):
            continue
        digest = question.get("input_digest")
        answers[str(conflict)] = ScopedAnswer(
            str(selection["option_key"]),
            str(digest) if digest else None,
            selection.get("delegated") is True,
            position,
        )
    return answers


def count_asks(events: Events, conflict_id: str, input_digest: str) -> int:
    """How many times this exact question (same conflict, same inputs) was asked."""

    return sum(
        1
        for role, payload in _event_list(events)
        if role == "assistant"
        and isinstance(payload, dict)
        and isinstance(payload.get("choice_question"), dict)
        and payload["choice_question"].get("conflict") == conflict_id
        and payload["choice_question"].get("input_digest") == input_digest
    )


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize_reply(text: object) -> str:
    """Case, punctuation and whitespace folded away: the only fuzz a typed reply gets."""

    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return _NON_WORD.sub(" ", folded).strip()


_RECOMMENDED_TAG = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def match_open_choice(question: Mapping[str, Any], message: object) -> str | None:
    """The option key a plain message names, or ``None``.

    Deterministic and exact: after normalisation the message must equal exactly ONE
    option's key, label, list number ("1", "option 2") or server-defined alias. Anything
    else (including a message that fits two options) is not an answer.
    """

    reply = normalize_reply(message)
    if not reply:
        return None
    hits: set[str] = set()
    for number, option in enumerate(question.get("options") or [], start=1):
        if not isinstance(option, Mapping) or not option.get("key"):
            continue
        label = _RECOMMENDED_TAG.sub("", str(option.get("label") or ""))
        names = {
            normalize_reply(option["key"]),
            normalize_reply(label),
            str(number),
            f"option {number}",
            f"number {number}",
            *(normalize_reply(a) for a in option.get("aliases") or []),
        }
        names.discard("")
        if reply in names:
            hits.add(str(option["key"]))
    return next(iter(hits)) if len(hits) == 1 else None


# Bare "whatever" / "I don't care" are often a non-answer, so they are NOT delegations.
_DELEGATION_PHRASES = frozenset(
    normalize_reply(p) for p in ("you choose", "you decide", "up to you", "surprise me")
)


def delegated_choice(question: Mapping[str, Any], message: object) -> str | None:
    """The recommended option key when the creator explicitly hands the choice over.

    Only a message that IS a delegation ("you choose", "up to you", "surprise me") counts;
    repeating the request, or any instruction, never does.
    """

    if normalize_reply(message) not in _DELEGATION_PHRASES:
        return None
    if question.get("conflict") == "creative_copy":
        # Delegating authorship is not permission to use unreviewed wording.
        return None
    if question.get("kind") == CONFLICT_TITLE_TEXT:
        # Handing over a wordless title would drop the title the creator asked for (and we
        # never write on-screen words): not a delegation. The gate asks once more.
        return None
    options = [o for o in question.get("options") or [] if isinstance(o, Mapping) and o.get("key")]
    if not options:
        return None
    pick = next((o for o in options if o.get("recommended")), options[0])
    return str(pick["key"])


@dataclass(frozen=True)
class ChoiceResolution:
    strategy: dict[str, Any]
    answers: tuple[dict[str, Any], ...]
    question: UnresolvedChoice | None
    notices: tuple[str, ...] = ()
    # Conflicts asked ``MAX_ASKS_PER_QUESTION`` times and still unanswered: the creator's
    # plan goes through UNCHANGED and the honest receipts / refusal say what is unmet.
    exhausted: tuple[UnresolvedChoice, ...] = ()
    # Answers dropped because the creator restated the requirement on a later turn (kept
    # as provenance only; a superseded answer never reaches the strategy or the pinned brief).
    superseded: tuple[str, ...] = ()


def _apply_effect(strategy: dict[str, Any], choice: UnresolvedChoice, option_key: str) -> None:
    for key, value in (choice.effects.get(option_key) or {}).get("strategy", {}).items():
        strategy[key] = value


_DISCLOSURES = {
    (CONFLICT_DURATION_VS_COUNT, OPT_EXTEND): (
        "I extended it so every clip stays on screen long enough to be seen."
    ),
    (CONFLICT_DURATION_VS_COUNT, OPT_FEWER): (
        "I kept your length and used fewer clips, spread evenly through your footage."
    ),
    (CONFLICT_ORDER_BASIS, OPT_ATTACHMENT_ORDER): ("I'm using the order you added the clips."),
    (CONFLICT_ORDER_BASIS, OPT_UNORDERED): "I'm not promising a particular order.",
    (CONFLICT_TITLE_TEXT, OPT_NO_TITLE): (
        "I'm leaving the title off, since you didn't give me the words."
    ),
    (CONFLICT_VOICE_VS_DURATION, OPT_MATCH_VOICE): "I ended the edit where your voice ends.",
    (CONFLICT_VOICE_VS_DURATION, OPT_SILENT_TAIL): (
        "I kept your length: the last seconds play without voice."
    ),
    (CONFLICT_VOICE_VS_DURATION, OPT_LENGTH_30): "I'm making it 30 seconds.",
    (CONFLICT_VOICE_VS_DURATION, OPT_LENGTH_60): "I'm making it 60 seconds.",
    **{
        (CONFLICT_WHICH_VOICE, f"clip_{i}"): "I'm using that clip's voice under the other clips."
        for i in range(1, MAX_CHOICE_OPTIONS + 1)
    },
}


def _emits_answered_value(
    data: Mapping[str, Any], choice: UnresolvedChoice, option_key: str
) -> bool:
    """Does the model's strategy already carry the answered length / clip subset?"""

    expected = (choice.effects.get(option_key) or {}).get("strategy", {})
    if "target_duration_s" in expected:
        seen = _number(data.get("target_duration_s", 24))
        if seen is None or abs(seen - float(expected["target_duration_s"])) > 0.05:
            return False
    if "selected_media_ids" in expected:
        if {str(m) for m in data.get("selected_media_ids") or []} != {
            str(m) for m in expected["selected_media_ids"]
        }:
            return False
    return True


def _answer_superseded(
    history: Sequence[tuple[str, Any]],
    prior: ScopedAnswer,
    choice: UnresolvedChoice,
    data: Mapping[str, Any],
) -> bool:
    """Did the creator restate the requirement AFTER answering?

    The answer wins on the turn(s) that follow the answer itself (the model may echo the
    option's label or re-emit the old value). On a LATER turn, a new user message that is
    not an answer, and not a verbatim re-send of an earlier message, together with a model
    plan that no longer carries the answered value, is the creator changing their mind:
    the answer is dropped and the gate evaluates afresh (it asks again, within the cap).
    """

    if choice.kind != CONFLICT_DURATION_VS_COUNT:
        return False

    def plain_user_text(payload: Any) -> bool:
        return isinstance(payload, dict) and not isinstance(payload.get("choice_selection"), dict)

    before = {
        payload.get(CONTENT_KEY)
        for role, payload in history[: prior.index]
        if role == "user" and plain_user_text(payload) and payload.get(CONTENT_KEY)
    }
    restated = [
        payload
        for role, payload in history[prior.index + 1 :]
        if role == "user"
        and plain_user_text(payload)
        and not (payload.get(CONTENT_KEY) and payload[CONTENT_KEY] in before)
    ]
    return bool(restated) and not _emits_answered_value(data, choice, prior.option_key)


def resolve_choices(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    events: Events,
    capability: ChoiceCapability | None = None,
) -> ChoiceResolution:
    """Apply earlier answers to the strategy and return at most ONE open question.

    * An answer counts only while its question's ``input_digest`` equals the digest the
      collector computes now: a changed media set or length reopens just that question.
    * The server-owned answer WINS: once answered, the chosen length / clip subset is
      written over whatever the model emitted on this turn (it may re-emit the old value
      or follow the option's label); the matching brief requirement is superseded in the
      pinned copy (``answered_brief``).
    * A LATER user message that restates the requirement (and a model plan without the
      answered value) supersedes the answer: see ``_answer_superseded``.
    * The same question is asked at most twice (``MAX_ASKS_PER_QUESTION``). After that
      nothing is chosen for the creator and nothing is rewritten: the conflict is returned
      in ``exhausted`` and the plan passes through (receipts or the backstop then state the
      unmet requirement). Only an explicit delegation ("you choose") picks the
      recommended option, recorded as ``source="creator_delegated"``.
    * Model-authored ``choice_answers`` are discarded: answers are server-owned.
    """

    data = _as_dict(strategy)
    data.pop("choice_answers", None)
    history = _event_list(events)
    scoped = fold_scoped_answers(history)
    answers: list[dict[str, Any]] = []
    notices: list[str] = []
    exhausted: list[UnresolvedChoice] = []
    superseded: list[str] = []
    for choice in collect_conflicts(data, brief, media_snapshot, capability):
        keys = {o.key for o in choice.options}
        prior = scoped.get(choice.conflict_id)
        valid = bool(
            prior and prior.input_digest == choice.input_digest and prior.option_key in keys
        )
        if valid and _answer_superseded(history, prior, choice, data):
            superseded.append(choice.conflict_id)
            valid = False
        if valid:
            option_key = prior.option_key
            source = "creator_delegated" if prior.delegated else "creator"
        elif count_asks(history, choice.conflict_id, choice.input_digest) >= MAX_ASKS_PER_QUESTION:
            exhausted.append(choice)
            continue
        else:
            return ChoiceResolution(
                data, tuple(answers), choice, tuple(notices), tuple(exhausted), tuple(superseded)
            )
        answers.append(
            {
                "conflict": choice.conflict_id,
                "kind": choice.kind,
                "option": option_key,
                "input_digest": choice.input_digest,
                "requirement_ids": list(choice.requirement_ids),
                "source": source,
            }
        )
        _apply_effect(data, choice, option_key)
        note = _DISCLOSURES.get((choice.kind, option_key))
        if note:
            notices.append(_disclose(note, source))
    if answers:
        data["choice_answers"] = answers
    return ChoiceResolution(
        data, tuple(answers), None, tuple(notices), tuple(exhausted), tuple(superseded)
    )


def _disclose(note: str, source: object) -> str:
    return (
        f"{note} (You left it to me, so I went with it.)" if source == "creator_delegated" else note
    )


def answered_brief(brief: Any, strategy: Any) -> Any:
    """``brief`` with the creator's answers applied to the requirements they resolve.

    A copy: the thread's stored brief keeps what the creator typed. The copy is what
    approval pins (``BriefBinding``), what draft receipts judge and what the render reads,
    so "Keep 15 seconds" does not keep failing a 24-second edit the creator chose.
    """

    data = _as_dict(strategy)
    answers = [a for a in data.get("choice_answers") or [] if isinstance(a, Mapping)]
    omitted = data.get("omitted_copy_targets") or []
    if brief is None or (not answers and not omitted):
        return brief
    changed: dict[str, Any] = {
        req.id: req.model_copy(update={"status": "superseded"})
        for req in _live(brief)
        if "opening_title" in omitted and req.kind == "text" and req.scope == "title"
    }
    for answer in answers:
        ids = {str(i) for i in answer.get("requirement_ids") or []}
        kind, option = answer.get("kind"), answer.get("option")
        for req in _live(brief):
            if req.id not in ids:
                continue
            if kind == CONFLICT_DURATION_VS_COUNT and option == OPT_EXTEND and req.kind == "timing":
                new = _number(data.get("target_duration_s"))
                old = _number((req.facts or {}).get("duration_s"))
                if new is None or old is None or abs(new - old) < 0.001:
                    continue
                changed[req.id] = req.model_copy(
                    update={
                        "facts": {**req.facts, "duration_s": data["target_duration_s"]},
                        "description": (
                            f"Keep {_clean_seconds(new)} seconds (extended from "
                            f"{_clean_seconds(old)} so every clip can be seen)"
                        ),
                    }
                )
            elif kind == CONFLICT_ORDER_BASIS and req.kind == "order":
                if option == OPT_ATTACHMENT_ORDER:
                    changed[req.id] = req.model_copy(
                        update={
                            "facts": {**req.facts, "key": ATTACHMENT_ORDER_KEY},
                            "description": "in the order the clips were added",
                        }
                    )
                elif option == OPT_UNORDERED:
                    changed[req.id] = req.model_copy(update={"status": "superseded"})
            elif (
                kind == CONFLICT_VOICE_VS_DURATION
                and option == OPT_MATCH_VOICE
                and req.kind == "timing"
            ):
                new = _number(data.get("target_duration_s"))
                old = _number((req.facts or {}).get("duration_s"))
                if new is None or old is None or abs(new - old) < 0.001:
                    continue
                changed[req.id] = req.model_copy(
                    update={
                        "facts": {**req.facts, "duration_s": data["target_duration_s"]},
                        "description": (
                            f"Keep {_clean_seconds(new)} seconds (as long as your voice, "
                            f"shortened from {_clean_seconds(old)})"
                        ),
                    }
                )
            elif (
                kind == CONFLICT_TITLE_TEXT
                and option == OPT_NO_TITLE
                and req.kind == "text"
                and req.scope == "title"
            ):
                changed[req.id] = req.model_copy(update={"status": "superseded"})
    if not changed:
        return brief
    return brief.model_copy(
        update={"requirements": [changed.get(r.id, r) for r in brief.requirements]}
    )


def choice_notices(strategy: Any) -> list[str]:
    """Plain-language disclosure of the decisions a strategy carries."""

    out: list[str] = []
    for answer in _as_dict(strategy).get("choice_answers") or []:
        if not isinstance(answer, Mapping):
            continue
        note = _DISCLOSURES.get((answer.get("kind"), answer.get("option")))
        if note:
            out.append(_disclose(note, answer.get("source")))
    return out


def ask_user_choice(
    question: str, reason_code: str, options: Sequence[str]
) -> tuple[str, dict[str, Any]] | None:
    """Turn the Creator agent's ``AskUser`` options into a tappable question.

    Needs at least two distinct options; otherwise the caller keeps the text-only question.
    The text lists the options too, so a client that cannot render the card still shows
    every choice. Returns ``(response_text, choice_question_payload)``.
    """

    labels: list[str] = []
    for raw in options:
        label = " ".join(str(raw).split())[:120]
        if label and label not in labels:
            labels.append(label)
    labels = labels[:MAX_CHOICE_OPTIONS]
    if len(labels) < 2:
        return None
    candidate = ConflictCandidate(
        conflict_id=f"ask_user:{reason_code}"[:80],
        reason="",
        intro=question.strip(),
        options=tuple(
            ConflictOption(key=f"option_{i}", label=text) for i, text in enumerate(labels, 1)
        ),
    )
    lines = [question.strip()]
    lines += [f"{i}. {label}" for i, label in enumerate(labels, start=1)]
    lines.append("Tap an option, or tell me in your own words.")
    text = "\n".join(lines)
    if len(text) > 1200:
        return None
    return text, build_choice_question(candidate)


__all__ = [
    "ATTACHMENT_ORDER_KEY",
    "CAPTURE_ORDER_KEYS",
    "CHOICE_QUESTION_VERSION",
    "CONFLICT_DURATION_VS_COUNT",
    "CONFLICT_ORDER_BASIS",
    "CONFLICT_PRIORITY",
    "CONFLICT_TEXT_PLACEMENT",
    "CONFLICT_TITLE_TEXT",
    "CONFLICT_VOICE_VS_DURATION",
    "CONFLICT_WHICH_VOICE",
    "KEEP_OPEN_REASON",
    "MAX_ASKS_PER_QUESTION",
    "OPT_ATTACHMENT_ORDER",
    "OPT_EXTEND",
    "OPT_FEWER",
    "OPT_LENGTH_30",
    "OPT_LENGTH_60",
    "OPT_MATCH_VOICE",
    "OPT_NO_TITLE",
    "OPT_SILENT_TAIL",
    "OPT_UNORDERED",
    "title_text_choice",
    "ChoiceCapability",
    "ChoiceResolution",
    "ScopedAnswer",
    "UnresolvedChoice",
    "answered_brief",
    "answered_conflict_ids",
    "ask_user_choice",
    "choice_notices",
    "collect_conflicts",
    "count_asks",
    "delegated_choice",
    "fold_scoped_answers",
    "match_open_choice",
    "normalize_reply",
    "open_conflicts",
    "resolve_choices",
    "CONFLICT_ORDER_VS_GROUP",
    "OPT_CHRONOLOGICAL",
    "OPT_GROUP_FIRST",
    "ORDER_VS_GROUP_OPTIONS",
    "ChoiceSelectionIn",
    "ConflictCandidate",
    "ConflictOption",
    "build_choice_question",
    "choice_question_text",
    "detect_order_vs_group",
    "fold_choice_answers",
    "latest_open_choice_question",
    "split_groups",
]
