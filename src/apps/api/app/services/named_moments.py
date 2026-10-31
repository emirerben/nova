"""KRI-723: the moments a creator names inside ONE take, and where each one really is.

"Six moments from it, in this order: Caro arriving with the black tray, her hello wave, ...":
the creator's words name actions, not windows. Before this module the length plan cut the take
into six ordinal energy windows and the receipt called the order met because six windows played
in take order. Nobody had located a single action.

Owner decision (2026-10-24): no paid video localizer and no new model call. So, per moment:

    creator words ──▶ stored evidence (analysed moments' start/end + description)
                          │
          one best match ─┼─ two equal matches ─┬─ no match
                 ▼        │          ▼          │      ▼
             grounded     │      ambiguous      │  unlocated
                          ▼                     ▼
               the planner asks the creator BEFORE approval to put the moments in order and
               point at each one (``moment_order_question``); the answer (``moment_order``)
               is checked by the server and folded from thread events into pins the length
               plan records (envelope v2) and the worker plays unchanged.

Only structured times count as evidence: ``analysis.best_moments`` / ``understanding.
notable_moments`` (``start_s`` / ``end_s`` / ``description``). Model prose, even prose with
timestamps in it, and a clip-level intent match never do. No regex reads the creator's words
(KRI-667): they are folded and split with the clip-order helpers.

Pure functions over stored data: no I/O, no settings, no model call.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.kria.brief_route import loose_text
from app.services.clip_order_sequence import _content, _words, sequence_rows

MOMENT_ORDER_QUESTION_KEY = "moment_order_question"
MOMENT_ORDER_ANSWER_KEY = "moment_order"

STALE_CODE = "moment_order_stale"
INVALID_CODE = "moment_order_invalid"
MEDIA_CODE = "moment_order_media_not_found"
OVERLAP_CODE = "moment_order_overlap"

# The shortest stretch a creator may point at: shorter reads as a flash, not a moment.
MIN_POINTED_S = 0.3
# A proxy's duration may run a frame or two past its original (``read_length_plan``).
_DURATION_SLACK_S = 0.1

# Window origins recorded on the length plan (``moments[].origin``).
ORIGIN_GROUNDED = "grounded"
ORIGIN_ANSWER = "creator_answer"
ORIGIN_DELEGATED = "ai_pick_by_request"
# A moment nobody located (an app that cannot answer the question): it plays the strongest
# free analysed stretch and its receipt says "couldn't find it", never met.
ORIGIN_UNLOCATED = "unlocated"
ORIGINS = frozenset({ORIGIN_GROUNDED, ORIGIN_ANSWER, ORIGIN_DELEGATED, ORIGIN_UNLOCATED})

# The answer as models read it (KRI-575 / KRI-694): no digits, no times, no request words.
MOMENT_ORDER_ANSWER_MESSAGE = "I showed you where each of my moments is."
MOMENT_ORDER_ANSWER_MESSAGE_TR = "Anlarımın her birinin nerede olduğunu gösterdim."

# Words this short or this generic never locate a moment ("the", "her", "with").
_MATCH_MIN_LEN = 3
_PREFIX_LEN = 4
_STOP = frozenset(
    {
        "the",
        "and",
        "with",
        "her",
        "his",
        "their",
        "its",
        "our",
        "your",
        "from",
        "into",
        "onto",
        "for",
        "then",
        "right",
        "just",
        "very",
        "this",
        "that",
        "ile",
        "bir",
        "sonra",
    }
)
# Words that open a listed item without naming anything ("..., and Caro's bye-bye wave").
_LEADING = frozenset({"and", "then", "ve", "sonra", "finally", "lastly"})


class MomentOrderError(ValueError):
    """An answer that cannot be applied. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- shared word matching (KRI-828's freeze scorer, now one implementation) ---------------------


def match_words(text: str) -> set[str]:
    """Folded words of ``text`` long enough to match an analysed moment's description."""
    folded = loose_text(str(text))
    return {
        word
        for word in "".join(ch if ch.isalnum() else " " for ch in folded).split()
        if len(word) >= _MATCH_MIN_LEN
    }


def same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _PREFIX_LEN and longer.startswith(shorter)


def pointed_match(target_words: Iterable[str], pointed: Sequence[str]) -> int | None:
    """KRI-723 x KRI-900: the one creator-pointed moment a freeze's words name, else None.

    ``pointed`` are the creator's words for each moment they pointed at (in order). A moment
    matches when it shares at least two of the freeze's words (one when it has only one);
    a tie names none. The freeze question (``choice_questions._freeze_frame``) and the render
    (``unified_montage._freeze_on_pinned_moment``) both use this, so they never disagree.
    """
    target = {word for word in target_words if len(word) >= _MATCH_MIN_LEN}
    if not target:
        return None
    need = min(2, len(target))
    scored = []
    for index, words in enumerate(pointed):
        found = match_words(words)
        score = sum(1 for word in target if any(same_word(word, other) for other in found))
        if score >= need:
            scored.append((score, index))
    best = max((score for score, _i in scored), default=0)
    top = [index for score, index in scored if score == best]
    return top[0] if len(top) == 1 else None


def pointed_moment_words(snapshot: Mapping[str, Any] | None) -> list[str]:
    """The creator's words for each moment they pointed at, from the length plan's record."""
    plan = (snapshot or {}).get("length_plan")
    rows = plan.get("moments") if isinstance(plan, Mapping) else None
    return [
        str(row.get("words") or "")
        for row in sorted(
            (r for r in rows or () if isinstance(r, Mapping)),
            key=lambda r: int(r.get("window_index", 0)),
        )
        if row.get("origin") == "creator_answer"
    ]


# -- which moments the creator named -----------------------------------------------------------


@dataclass(frozen=True)
class NamedMoment:
    key: str
    words: str
    row: int  # index into ``sequence_rows`` (several moments may share a listing row)


@dataclass(frozen=True)
class MomentSet:
    take: str
    generation: str
    duration_s: float
    analysis: Mapping[str, Any]
    moments: tuple[NamedMoment, ...]
    visual_rows: tuple[int, ...]
    source_path: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(moment.key for moment in self.moments)

    @property
    def digest(self) -> str:
        return _digest({"take": self.take, "generation": self.generation, "keys": self.keys})


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def moment_key(words: str, media_id: str, generation: str) -> str:
    """Identity of one named moment: the creator's folded words on this exact file.

    A reworded moment or a re-uploaded take is a different moment, so its old pin never
    carries over (F11)."""
    return _digest({"words": " ".join(_words(words)), "media_id": media_id, "g": generation})


def _listed(name: str) -> list[str]:
    """The moments one row lists, comma by comma, in the creator's own words."""
    out: list[str] = []
    for part in name.replace(";", ",").split(","):
        words = part.strip().split()
        while words and loose_text(words[0]) in _LEADING:
            words = words[1:]
        text = " ".join(words).strip(" .")
        if text and _content(_words(text)):
            out.append(text)
    return out


def named_moments(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any] | None,
    *,
    moment_count: int | None = None,
) -> MomentSet | None:
    """The moments the creator named inside the edit's one take, else None.

    It applies when the creator's stated sequence (server-verified ``resolved_clip_intents``)
    is all resolved ``then`` rows, the footage they name is ONE video, which is the only video
    in the edit, and the rows name at least two moments of it: one row listing them ("A, B,
    C") or one row per moment. Rows naming only other media (a Visual photo) ride along. A
    moment count that disagrees with the named moments raises a bounded recovery error.
    """
    if not isinstance(media_snapshot, Mapping):
        return None
    if strategy.get("edit_format") != "montage":
        return None
    rows = sequence_rows(strategy.get("resolved_clip_intents"))
    if not rows or any(status != "resolved" for _pos, _n, _m, status in rows):
        return None
    videos = {
        str(row.get("media_id")): row
        for row in media_snapshot.get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("kind", "video") == "video" and row.get("media_id")
    }
    selected = {str(v) for v in strategy.get("selected_media_ids") or []}
    if selected:
        videos = {media_id: row for media_id, row in videos.items() if media_id in selected}
    # The length plan pins moments only in an all-video edit: never ask what it cannot play.
    if len(videos) != 1 or any(
        isinstance(r, Mapping)
        and r.get("kind", "video") != "video"
        and (not selected or str(r.get("media_id")) in selected)
        for r in media_snapshot.get("clip_assignments") or []
    ):
        return None
    take, row = next(iter(videos.items()))
    moments: list[NamedMoment] = []
    visual_rows: list[int] = []
    generation = str(row.get("storage_generation") or row.get("generation") or "")
    for index, (position, name, members, _status) in enumerate(rows):
        if set(members) == {take}:
            if position is not None:
                return None
            for words in _listed(name):
                moments.append(NamedMoment(moment_key(words, take, generation), words, index))
        elif members and take not in members:
            visual_rows.append(index)
        else:
            return None
    keys = [moment.key for moment in moments]
    if len(moments) < 2 or len(set(keys)) != len(keys):
        return None
    if moment_count is not None and moment_count != len(moments):
        from app.kria.reply_language import say  # noqa: PLC0415

        raise MomentOrderError(
            "moment_order_count_mismatch",
            say(
                en=(
                    f"You asked for {moment_count} moments from the video, but I matched "
                    f"{len(moments)} to it. Please tell me which moments to keep or correct "
                    "the clip choices before I prepare the edit."
                ),
                tr=(
                    f"Videodan {moment_count} an istedin, ancak {len(moments)} tanesini "
                    "videoyla eşleştirdim. Düzenlemeyi hazırlamadan önce hangi anları "
                    "tutacağımı belirtir veya klip seçimlerini düzeltir misin?"
                ),
            ),
        )
    duration = row.get("duration_s")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return None
    analysis = row.get("analysis") if isinstance(row.get("analysis"), Mapping) else {}
    return MomentSet(
        take=take,
        generation=generation,
        duration_s=float(duration),
        analysis=analysis,
        moments=tuple(moments),
        visual_rows=tuple(visual_rows),
        source_path=str(row.get("gcs_path") or "") or None,
    )


# -- grounding from stored evidence only -------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    start_s: float
    end_s: float
    description: str
    kind: str
    evidence: Mapping[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return {
            "source_start_s": self.start_s,
            "source_end_s": self.end_s,
            "description": self.description,
            "kind": self.kind,
            **({"evidence": dict(self.evidence)} if self.evidence else {}),
        }


@dataclass(frozen=True)
class Grounding:
    status: Literal["grounded", "ambiguous", "unlocated"]
    found: Interval | None = None
    suggestions: tuple[Interval, ...] = ()


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _evidence(analysis: Mapping[str, Any], duration_s: float) -> list[Interval]:
    rows: list[tuple[Any, str]] = [
        (row, "analysed_moment") for row in analysis.get("best_moments") or []
    ]
    understanding = analysis.get("understanding")
    if isinstance(understanding, Mapping):
        rows += [(row, "notable_moment") for row in understanding.get("notable_moments") or []]
    out: dict[tuple[float, float], Interval] = {}
    for row, kind in rows:
        if not isinstance(row, Mapping):
            continue
        start, end = _number(row.get("start_s")), _number(row.get("end_s"))
        description = str(row.get("description") or "").strip()
        if start is None or end is None or not description:
            continue
        start, end = max(0.0, start), min(duration_s, end)
        if end - start < MIN_POINTED_S:
            continue
        span = (round(start, 3), round(end, 3))
        out.setdefault(span, Interval(span[0], span[1], description, kind))
    return list(out.values())


def _terms(text: str) -> set[str]:
    return {word for word in match_words(text) if word not in _STOP}


def ground(words: str, analysis: Mapping[str, Any], duration_s: float) -> Grounding:
    """Where ``words`` happen in the take, from the stored clip record alone.

    A described interval matches when it shares enough of the creator's content words (two,
    or the only one there is). One best match is ``grounded``; two equal best matches are
    ``ambiguous`` (both offered, neither chosen); none is ``unlocated``.
    """
    target = _terms(words)
    if not target:
        return Grounding("unlocated")
    need = min(2, len(target))
    scored: list[tuple[int, Interval]] = []
    for interval in _evidence(analysis, duration_s):
        described = _terms(interval.description)
        score = sum(1 for word in target if any(same_word(word, other) for other in described))
        if score >= need:
            scored.append((score, interval))
    if not scored:
        return Grounding("unlocated")
    best = max(score for score, _i in scored)
    top = sorted((i for score, i in scored if score == best), key=lambda i: (i.start_s, i.end_s))
    if len(top) == 1:
        return Grounding("grounded", found=top[0])
    return Grounding("ambiguous", suggestions=tuple(top))


# Words that say WHERE or HOW MUCH, never WHAT happens: inside one clip they name no moment.
_PLACE_WORDS = frozenset(
    {"moment", "moments", "clip", "clips", "video", "videos", "shot", "shots", "take", "takes"}
    | {"part", "parts", "bit", "bits", "scene", "scenes", "footage", "where", "when", "both"}
    | {"each", "every", "only", "end", "ending", "start", "beginning", "middle", "first", "last"}
    | {"second", "third"}
    | {"klip", "klibi", "klibin", "klipte", "klipler", "video", "videoda", "videonun", "sahne"}
    | {"kisim", "kismi", "bolum", "son", "sonu", "sonunda", "bas", "basi", "basinda", "ikinci"}
)


def moment_terms(words: str) -> set[str]:
    """The words that could name what happens ("taha", "scream"), never where ("end", "clip")."""
    return {word for word in _terms(words) if word not in _PLACE_WORDS}


def locate_in_clip(words: str, analysis: Mapping[str, Any], duration_s: float) -> Grounding:
    """Leverkusen M2 run 3: where a moment the creator named lies inside ONE clip they confirmed.

    "Then the TOR on the big screen" (the creator tapped clip 2 for it), "put Taha's scream at
    the end of clip 2 in slow motion": the creator already said which clip, so the search is
    that clip's own stored moments only (``_evidence``: analysed + notable moments, never
    prose) and one shared content word places it. Words that only say where or how much ("the
    end of", "moments", "both goal clips") never count. One best interval is ``grounded``; two
    equal best are ``ambiguous``; none (or a record with no moments at all, IMG_1039's
    ``best_moments: []``) is ``unlocated``, and the caller says so instead of guessing.
    """
    target = moment_terms(words)
    if not target or not isinstance(analysis, Mapping) or duration_s <= 0:
        return Grounding("unlocated")
    scored: list[tuple[int, Interval]] = []
    for interval in _evidence(analysis, duration_s):
        described = _terms(interval.description)
        score = sum(1 for word in target if any(same_word(word, other) for other in described))
        if score:
            scored.append((score, interval))
    if not scored:
        return Grounding("unlocated")
    best = max(score for score, _i in scored)
    top = sorted((i for score, i in scored if score == best), key=lambda i: (i.start_s, i.end_s))
    if len(top) == 1:
        return Grounding("grounded", found=top[0])
    return Grounding("ambiguous", suggestions=tuple(top))


def shows(start_s: float, end_s: float, found: Interval) -> bool:
    """Does a cut playing ``start_s``..``end_s`` show the located moment? Half of the shorter
    of the two must overlap (KRI-723's ``_MOMENT_COVER``)."""
    shown = min(end_s, found.end_s) - max(start_s, found.start_s)
    return shown + 1e-6 >= 0.5 * min(end_s - start_s, found.end_s - found.start_s)


# A cut this close to its clip's full length plays the whole clip, so it shows whatever the
# clip holds (a frame or two of proxy slack, KRI-217's tiny-clip rule).
WHOLE_CLIP_SLACK_S = 0.25


@dataclass(frozen=True)
class ClipMoment:
    """A moment the creator named in their stated order, inside one video of a multi-clip
    edit (Leverkusen M2 run 3: "then the TOR on the big screen", clip 2 tapped for it).

    ``tapped`` says the creator's own tap in the clip picker put this clip under the words:
    nothing in the clip record matched them, so only a located moment shows the cut has it.
    """

    words: str
    media_id: str
    tapped: bool
    grounding: Grounding


def clip_moments(
    strategy: Mapping[str, Any], media_snapshot: Mapping[str, Any] | None
) -> list[ClipMoment]:
    """Every (named moment, video) pair of the creator's stated sequence, located from the
    clip records alone, in the order the creator listed them. Empty where KRI-723's one-take
    moments apply (``named_moments``), and for words that name no moment ("then clip 2")."""
    from app.schemas.clip_intents import CREATOR_SELECTED_EVIDENCE  # noqa: PLC0415

    if not isinstance(media_snapshot, Mapping) or named_moments(strategy, media_snapshot):
        return []
    videos = {
        str(row.get("media_id")): row
        for row in media_snapshot.get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("kind", "video") == "video" and row.get("media_id")
    }
    out: list[ClipMoment] = []
    for intent in strategy.get("resolved_clip_intents") or []:
        if (
            not isinstance(intent, Mapping)
            or intent.get("op") != "order"
            or intent.get("order_by")
            or intent.get("placeholder")
            or intent.get("position") not in (None, "first", "last")
            or str(intent.get("status") or "resolved") != "resolved"
        ):
            continue
        words = str(intent.get("attribute") or "").strip()
        if not moment_terms(words):
            continue
        for assignment in intent.get("assignments") or []:
            if not isinstance(assignment, Mapping):
                continue
            row = videos.get(str(assignment.get("media_id") or ""))
            duration = _number(row.get("duration_s")) if row is not None else None
            if row is None or duration is None or duration <= 0:
                continue
            analysis = row.get("analysis") if isinstance(row.get("analysis"), Mapping) else {}
            out.append(
                ClipMoment(
                    words=words,
                    media_id=str(row["media_id"]),
                    tapped=assignment.get("evidence") == CREATOR_SELECTED_EVIDENCE,
                    grounding=locate_in_clip(words, analysis, duration),
                )
            )
    return out


def ground_all(moment_set: MomentSet) -> dict[str, Grounding]:
    """Every named moment's grounding. Two moments that land on the same interval are both
    ambiguous: one interval cannot be two different actions."""
    out = {
        moment.key: ground(moment.words, moment_set.analysis, moment_set.duration_s)
        for moment in moment_set.moments
    }
    claims: dict[tuple[float, float], list[str]] = {}
    for key, found in out.items():
        if found.found is not None:
            claims.setdefault((found.found.start_s, found.found.end_s), []).append(key)
    for keys in claims.values():
        if len(keys) > 1:
            for key in keys:
                out[key] = Grounding("ambiguous", suggestions=(out[key].found,))  # type: ignore[arg-type]
    return out


# -- the creator's answer ----------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """One moment as the approved plan records it (``length_plan.moments[]``)."""

    key: str
    words: str
    row: int
    origin: str
    start_s: float | None = None
    end_s: float | None = None
    evidence: Mapping[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return {
            "moment_key": self.key,
            "words": self.words,
            "row": self.row,
            "origin": self.origin,
            "source_start_s": self.start_s,
            "source_end_s": self.end_s,
            **({"evidence": dict(self.evidence)} if self.evidence else {}),
        }


@dataclass(frozen=True)
class AnsweredMoment:
    action: Literal["point", "skip", "delegate"]
    start_s: float | None = None
    end_s: float | None = None


@dataclass
class MomentFold:
    """Every valid answer folded (latest wins per moment) and the moment sets already asked."""

    answers: dict[str, AnsweredMoment] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    asked: set[str] = field(default_factory=set)


def _float(value: object) -> float | None:
    return _number(value)


def validate_moment_order_answer(
    answer: Mapping[str, Any] | None, question: Mapping[str, Any] | None
) -> None:
    """Raise ``MomentOrderError`` unless ``answer`` answers ``question`` completely and soundly.

    Every asked moment exactly once (in the creator's final order); a pointed window on the
    asked take, finite, inside the original's length, at least ``MIN_POINTED_S`` long, and not
    overlapping another pointed window (the envelope refuses overlaps; say so here, early).
    """
    if answer is None:
        return
    if not isinstance(question, Mapping) or question.get("question_id") != answer.get(
        "question_id"
    ):
        raise MomentOrderError(
            STALE_CODE, "That moments question is no longer open. Refresh and answer the latest."
        )
    asked = [str(row.get("moment_key")) for row in question.get("moments") or []]
    rows = answer.get("answers")
    if not isinstance(rows, list):
        raise MomentOrderError(INVALID_CODE, "That answer has no moments in it.")
    keys = [str(row.get("moment_key")) if isinstance(row, Mapping) else "" for row in rows]
    if len(set(keys)) != len(keys) or set(keys) != set(asked):
        raise MomentOrderError(INVALID_CODE, "Answer every moment I asked about, once each.")
    take = question.get("take") if isinstance(question.get("take"), Mapping) else {}
    duration = _float(take.get("duration_s")) or 0.0
    spans: list[tuple[float, float]] = []
    for row in rows:
        action = row.get("action")
        if action in ("skip", "delegate"):
            continue
        if action != "point":
            raise MomentOrderError(
                INVALID_CODE, "Each moment is pointed at, skipped or left to me."
            )
        if str(row.get("media_id") or "") != str(take.get("media_id") or ""):
            raise MomentOrderError(MEDIA_CODE, "That clip isn't part of this video.")
        start, end = _float(row.get("source_start_s")), _float(row.get("source_end_s"))
        if (
            start is None
            or end is None
            or start < 0
            or end > duration + _DURATION_SLACK_S
            or end - start < MIN_POINTED_S - 1e-9
        ):
            raise MomentOrderError(INVALID_CODE, "That moment is outside the clip or too short.")
        if any(
            start < other_end - 0.001 and end > other_start + 0.001
            for other_start, other_end in spans
        ):
            raise MomentOrderError(OVERLAP_CODE, "Two of your moments overlap. Move one of them.")
        spans.append((start, end))


def latest_open_moment_question(events: Iterable[tuple[str, Any]]) -> Mapping[str, Any] | None:
    """The latest ``moment_order_question`` not yet answered by a ``moment_order``."""
    latest: Mapping[str, Any] | None = None
    for event in events:
        role, payload = event[0], event[1]
        if not isinstance(payload, Mapping):
            continue
        if role == "assistant" and isinstance(payload.get(MOMENT_ORDER_QUESTION_KEY), Mapping):
            latest = payload[MOMENT_ORDER_QUESTION_KEY]
        elif role == "user" and isinstance(payload.get(MOMENT_ORDER_ANSWER_KEY), Mapping):
            if latest is not None and (
                payload[MOMENT_ORDER_ANSWER_KEY].get("question_id") == latest.get("question_id")
            ):
                latest = None
    return latest


def fold_moment_orders(events: Iterable[tuple[str, Any]]) -> MomentFold:
    """Replay question/answer events (chronological). Only answers to a question the server
    really asked, that pass ``validate_moment_order_answer``, count; per moment the latest wins,
    and the latest answer's row order is the creator's order."""
    questions: dict[str, Mapping[str, Any]] = {}
    fold = MomentFold()
    for event in events:
        role, payload = event[0], event[1]
        if not isinstance(payload, Mapping):
            continue
        question = payload.get(MOMENT_ORDER_QUESTION_KEY) if role == "assistant" else None
        if isinstance(question, Mapping):
            questions[str(question.get("question_id"))] = question
            fold.asked.add(str(question.get("moment_set")))
            continue
        answer = payload.get(MOMENT_ORDER_ANSWER_KEY) if role == "user" else None
        if not isinstance(answer, Mapping):
            continue
        asked = questions.get(str(answer.get("question_id")))
        try:
            validate_moment_order_answer(answer, asked)
        except MomentOrderError:
            continue
        fold.order = []
        for row in answer["answers"]:
            key = str(row["moment_key"])
            fold.order.append(key)
            fold.answers[key] = AnsweredMoment(
                action=row["action"],
                start_s=_float(row.get("source_start_s")),
                end_s=_float(row.get("source_end_s")),
            )
    return fold


def model_echo(language: str | None) -> str:
    """The neutral line models read for a ``moment_order`` answer."""
    return MOMENT_ORDER_ANSWER_MESSAGE_TR if language == "tr" else MOMENT_ORDER_ANSWER_MESSAGE


# -- the gate's decision -----------------------------------------------------------------------


@dataclass(frozen=True)
class MomentDecision:
    """``question`` to ask before approval, or ``pins`` the length plan records (in the
    creator's final order; skipped moments in ``skipped``), or neither (no named moments, or an
    app that cannot answer: today's windows with an honest receipt)."""

    question: dict[str, Any] | None = None
    recovery: str | None = None
    pins: tuple[Pin, ...] = ()
    skipped: tuple[Pin, ...] = ()
    found: int = 0
    total: int = 0


@dataclass(frozen=True)
class InspectedGroundings:
    moment_set_digest: str
    groundings: Mapping[str, Grounding]


def decide(
    moment_set: MomentSet | None,
    events: Iterable[tuple[str, Any]],
    *,
    can_ask: bool,
    default_window_s: float,
    inspections: InspectedGroundings | None = None,
) -> MomentDecision:
    if moment_set is None:
        return MomentDecision()
    fold = fold_moment_orders(events)
    groundings = ground_all(moment_set)
    if inspections is not None and inspections.moment_set_digest == moment_set.digest:
        for key, result in inspections.groundings.items():
            if key in groundings:
                groundings[key] = result
        located = [(key, result.found) for key, result in groundings.items() if result.found]
        conflicts = set()
        for index, (key, interval) in enumerate(located):
            for other_key, other in located[index + 1 :]:
                if (
                    interval.start_s < other.end_s - 0.001
                    and interval.end_s > other.start_s + 0.001
                ):
                    conflicts.update((key, other_key))
        for key, interval in located:
            if key in conflicts:
                groundings[key] = Grounding("ambiguous", suggestions=(interval,))
    by_key = {moment.key: moment for moment in moment_set.moments}
    answered = {key: fold.answers[key] for key in by_key if key in fold.answers}
    found = sum(1 for g in groundings.values() if g.status == "grounded")
    if len(answered) == len(by_key):
        order = [key for key in fold.order if key in by_key]
        # Answers to an earlier question (a moment reworded back) keep their place too.
        order += [key for key in by_key if key not in order]
        return _pins(moment_set, order, answered, groundings, found)
    if not can_ask:
        # Dark with the question (moment_order_min_client_protocol): an app that cannot answer
        # keeps today's v1 windows byte-identical, with an honest receipt.
        return MomentDecision(found=found, total=len(by_key))
    missing = [
        key for key in by_key if key not in answered and groundings[key].status != "grounded"
    ]
    if not missing:
        return _pins(moment_set, list(by_key), answered, groundings, found)
    # Asked until every moment is pointed at, skipped or left to Kria (owner decision
    # 2026-10-24): an unanswered card never turns into guessed windows.
    return MomentDecision(
        question=_question(moment_set, groundings, answered, default_window_s),
        found=found,
        total=len(by_key),
    )


def _pins(
    moment_set: MomentSet,
    order: Sequence[str],
    answered: Mapping[str, AnsweredMoment],
    groundings: Mapping[str, Grounding],
    found: int,
) -> MomentDecision:
    by_key = {moment.key: moment for moment in moment_set.moments}
    pins: list[Pin] = []
    skipped: list[Pin] = []
    for key in order:
        moment = by_key[key]
        reply = answered.get(key)
        if reply is not None and reply.action == "skip":
            skipped.append(Pin(key, moment.words, moment.row, "skipped"))
        elif reply is not None and reply.action == "delegate":
            pins.append(Pin(key, moment.words, moment.row, ORIGIN_DELEGATED))
        elif reply is not None:
            # A proxy's clock may run a frame past the original: the window ends at the take.
            end = reply.end_s if reply.end_s is None else min(reply.end_s, moment_set.duration_s)
            pins.append(
                Pin(
                    key,
                    moment.words,
                    moment.row,
                    ORIGIN_ANSWER,
                    reply.start_s,
                    end,
                    {"kind": "creator_answer"},
                )
            )
        elif groundings[key].status != "grounded" or groundings[key].found is None:
            pins.append(Pin(key, moment.words, moment.row, ORIGIN_UNLOCATED))
        else:
            interval = groundings[key].found
            assert interval is not None
            pins.append(
                Pin(
                    key,
                    moment.words,
                    moment.row,
                    ORIGIN_GROUNDED,
                    interval.start_s,
                    interval.end_s,
                    interval.json(),
                )
            )
    return MomentDecision(pins=tuple(pins), skipped=tuple(skipped), found=found, total=len(by_key))


def _question(
    moment_set: MomentSet,
    groundings: Mapping[str, Grounding],
    answered: Mapping[str, AnsweredMoment],
    default_window_s: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for moment in moment_set.moments:
        reply = answered.get(moment.key)
        found = groundings[moment.key]
        if reply is not None and reply.action == "point":
            status, interval = (
                "answered",
                {
                    "source_start_s": reply.start_s,
                    "source_end_s": reply.end_s,
                    "description": None,
                    "kind": "creator_answer",
                },
            )
        else:
            status = found.status
            interval = found.found.json() if found.found is not None else None
        rows.append(
            {
                "moment_key": moment.key,
                "words": moment.words,
                "status": status,
                "found": interval,
                "suggestions": [s.json() for s in found.suggestions],
            }
        )
    body = {
        "moment_set": moment_set.digest,
        "take": {
            "media_id": moment_set.take,
            "generation": moment_set.generation,
            "duration_s": moment_set.duration_s,
        },
        "default_window_s": round(float(default_window_s), 3),
        "moments": rows,
    }
    return {"question_id": f"moments-{_digest(body)}", **body}


def question_text(question: Mapping[str, Any]) -> str:
    """The short chat line above the card, in the chat's language."""
    from app.kria.reply_language import say  # noqa: PLC0415 - keeps this module import-light

    rows = [row for row in question.get("moments") or [] if isinstance(row, Mapping)]
    total = len(rows)
    found = sum(1 for row in rows if row.get("status") in ("grounded", "answered"))
    return say(
        en=(
            f"I found {found} of your {total} moments in the clip. Put them in order and show "
            "me where each one is, and I'll cut exactly those."
        ),
        tr=(
            f"Saydığın {total} andan {found} tanesini klipte buldum. Onları sıraya koy ve her "
            "birinin nerede olduğunu göster, tam olarak onları keseyim."
        ),
    )
