"""Did the creator ask the video to start at a line they quote? (KRI-628)

"Cut the 'Hazırlıklara başladın mı' part and the long pauses; start the video with the
'Abonelerimize bir şey demek ister misin' question" lands in the Creative Brief as a
``select`` requirement with no facts. The Main Creator strategy has no field for it, so
(like `speaker_framing_ask`) the ask is read deterministically off the requirement's own
words by the phone Talking worker (to cut everything before the line,
`app.pipeline.phone_opening_cut`) and by `app.kria.brief_checks` (to answer the
requirement from the render's receipt).

What counts: a quoted line of at least two words that sits right next to a "start" cue --
"start the video at/with “…”", "begin with “…”", "open on “…”", "cut everything before
“…”", "“…” sorusuyla başlasın", "“…” ile başlasın", "“…” öncesini kes". A quote beside no
such cue (the part to cut, a title, a caption word) is never the start line.

KRI-790: "start at '…' and end right after '…'" is recorded by the brief writer as a
``timing`` requirement's ``facts.start_line`` / ``facts.end_line`` (the creator's exact lines,
KRI-789). Briefs stored before that carry them as ``facts.start`` / ``facts.end`` on an
``order`` requirement (Merope T1), which on a one-take Talking edit can only mean the same.
Those fields are read as they are, never re-parsed from the words (KRI-667): the start line
from them or from the cue above, the end line only from a field. ``estimate_take_window``
places both in the take's timed speech before approval, with the same cut the worker makes.

KRI-837: a line in ``start_line`` / ``end_line`` may be one word ("Cheers!" ends a take); the
two-word floor stays for the cue reader and the legacy ``start`` / ``end`` keys, whose one
words are titles, signs and a route's places.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.kria.brief_route import loose_text
from app.services.spoken_quotes import MIN_QUOTE_TOKENS, fold_tokens, quoted_spans

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# Other kinds (text, order, audio) have their own checkers; a literal title is not speech.
OPENING_CUT_KINDS = frozenset({"select", "timing", "style"})
# KRI-790: the brief fields holding the creator's start / end line (KRI-789's shape), and the
# legacy keys an `order` requirement stored them under before it (route endpoints on a montage
# use those keys too; only a one-take Talking edit reads them as lines).
START_LINE_FACT = "start_line"
END_LINE_FACT = "end_line"
_LEGACY_START_FACT = "start"
_LEGACY_END_FACT = "end"
_LINE_FACT_KINDS = OPENING_CUT_KINDS | {"order"}

# A cue and its quote are at most this many words apart ("start the video at “…”",
# "“…” sorusuyla başlasın").
_MAX_GAP_WORDS = 4

_CUE_PATTERNS = (
    # English
    re.compile(r"^(?:start|starts|starting|begin|begins|beginning|kick|kicks)$"),
    re.compile(r"^(?:open|opens|opening)$"),
    # Turkish: başla / başlasın / başlat / başlayalım; açılsın / açılmalı
    re.compile(r"^basla\w*$"),
    re.compile(r"^(?:acilsin|acilmali|acilacak)$"),
)
# "cut everything before “…”", "“…” öncesini kes", "“…” öncesi"
_BEFORE_CUT = re.compile(
    r"\b(?:cut|trim|remove|delete|drop|skip|chop)\b(?:\s+\w+){0,3}?\s+(?:before|until|up\s+to)\s+$"
)
_TR_BEFORE = re.compile(r"^(?:oncesi\w*|onceki\w*)$")
# A quote beside "title"/"caption" is on-screen text, not a line to start the take at.
_ON_SCREEN_WORDS = frozenset(
    {"title", "titles", "caption", "captions", "text", "label", "badge", "baslik", "basligi"}
    | {"altyazi", "yazi", "yazisi", "rozet", "rozeti", "metin", "metni"}
)
_PLACEHOLDER = re.compile(r"^qq(\d+)qq$")


@dataclass(frozen=True)
class OpeningCutAsk:
    """The line the video must start at, and the brief requirements that asked for it."""

    quote: str
    requirement_ids: tuple[str, ...]


def opening_quote(text: str | None) -> str | None:
    """The quoted line ``text`` says the video should start at, else None."""
    raw = str(text or "")
    spans = quoted_spans(raw)
    if not spans:
        return None
    pieces: list[str] = []
    cursor = 0
    for index, (start, end, _quote) in enumerate(spans):
        pieces.append(raw[cursor:start])
        pieces.append(f" qq{index}qq ")
        cursor = end
    pieces.append(raw[cursor:])
    folded = loose_text("".join(pieces))
    words = re.findall(r"qq\d+qq|\w+", folded)
    holders = {
        int(match.group(1)): i
        for i, word in enumerate(words)
        if (match := _PLACEHOLDER.match(word)) is not None
    }
    best: tuple[int, int] | None = None  # (distance, quote index)
    for i, word in enumerate(words):
        if not any(pattern.match(word) for pattern in _CUE_PATTERNS) and not _TR_BEFORE.match(word):
            continue
        for index, position in holders.items():
            gap = abs(position - i) - 1
            if gap > _MAX_GAP_WORDS:
                continue
            between = words[min(i, position) + 1 : max(i, position)]
            before_quote = words[max(0, position - 3) : position]
            if _ON_SCREEN_WORDS.intersection(between) or _ON_SCREEN_WORDS.intersection(
                before_quote
            ):
                continue
            if best is None or (gap, index) < best:
                best = (gap, index)
    for position in holders.values():
        lead = " ".join(words[max(0, position - 6) : position]) + " "
        if _BEFORE_CUT.search(lead):
            index = next(idx for idx, pos in holders.items() if pos == position)
            if best is None or (0, index) < best:
                best = (0, index)
    if best is None:
        return None
    quote = spans[best[1]][2].strip()
    return quote if len(fold_tokens(quote)) >= MIN_QUOTE_TOKENS else None


def _line_keys(req: BriefRequirement, key: str, legacy_key: str) -> tuple[tuple[str, int], ...]:
    """The fact keys a side is read from, each with the fewest folded words it holds as a line.

    KRI-837: the dedicated ``start_line`` / ``end_line`` keys hold the creator's own quoted
    words, so one word is a line ("Cheers!" ends a take). The legacy ``start`` / ``end`` keys
    (read on `order` rows only) keep the two-word floor: they are also a route's one-word
    places (Arnavutköy -> Eminönü), which are never lines in a take.
    """
    if req.kind == "order":
        return ((key, 1), (legacy_key, MIN_QUOTE_TOKENS))
    return ((key, 1),)


def _fact_line(req: BriefRequirement, key: str, legacy_key: str) -> str | None:
    """The line stored under ``key``, else (on an `order` requirement) ``legacy_key``."""
    if req.kind not in _LINE_FACT_KINDS or not isinstance(req.facts, Mapping):
        return None
    for name, floor in _line_keys(req, key, legacy_key):
        value = req.facts.get(name)
        line = value.strip() if isinstance(value, str) else ""
        if len(fold_tokens(line)) >= floor:
            return line
    return None


def requirement_opening_quote(req: BriefRequirement) -> str | None:
    """A live requirement's start line: its start-line fact, else the quote its words ask
    the video to start at."""
    if not req.live:
        return None
    line = _fact_line(req, START_LINE_FACT, _LEGACY_START_FACT)
    if line is not None:
        return line
    if req.kind not in OPENING_CUT_KINDS:
        return None
    return opening_quote(req.description)


def requirement_end_quote(req: BriefRequirement) -> str | None:
    """A live requirement's end line (its end-line fact): the video ends right after it."""
    return _fact_line(req, END_LINE_FACT, _LEGACY_END_FACT) if req.live else None


def wants_take_window(req: BriefRequirement) -> bool:
    """True when ``req`` asks the take to start and/or end at a line the creator quotes."""
    return requirement_opening_quote(req) is not None or requirement_end_quote(req) is not None


def unread_line_sides(req: BriefRequirement) -> tuple[bool, bool]:
    """(start, end): True for a side the creator wrote into a fact the reader consults, where
    it holds fewer words than that key needs (`_line_keys`: no words in ``start_line`` /
    ``end_line``, one word in a legacy ``start`` / ``end``), and no reader gives the side a line.

    KRI-837: the creator still asked for that side. The receipt keeps it unchecked instead of
    certifying the ask from the other side's cut alone ("Cheers!" under a legacy ``end``).
    """
    if not req.live or req.kind not in _LINE_FACT_KINDS or not isinstance(req.facts, Mapping):
        return False, False

    def _unread(key: str, legacy_key: str, read: Callable[[BriefRequirement], str | None]) -> bool:
        if read(req) is not None:
            return False
        for name, floor in _line_keys(req, key, legacy_key):
            value = req.facts.get(name)
            if isinstance(value, str) and value.strip() and len(fold_tokens(value)) < floor:
                return True
        return False

    return (
        _unread(START_LINE_FACT, _LEGACY_START_FACT, requirement_opening_quote),
        _unread(END_LINE_FACT, _LEGACY_END_FACT, requirement_end_quote),
    )


def _first_line(
    requirements: Iterable[BriefRequirement], read: Callable[[BriefRequirement], str | None]
) -> tuple[str, tuple[str, ...]] | None:
    """The first line ``read`` finds, and every requirement asking for that same line (so
    each gets its receipt)."""
    found = [(req.id, line) for req in requirements if (line := read(req)) is not None]
    if not found:
        return None
    quote = found[0][1]
    key = fold_tokens(quote)
    return quote, tuple(req_id for req_id, other in found if fold_tokens(other) == key)


def opening_cut_ask(requirements: Iterable[BriefRequirement]) -> OpeningCutAsk | None:
    """The first start-at-a-line ask among ``requirements``."""
    found = _first_line(requirements, requirement_opening_quote)
    return OpeningCutAsk(quote=found[0], requirement_ids=found[1]) if found else None


def brief_opening_cut(brief: CreativeBrief | None) -> OpeningCutAsk | None:
    """`opening_cut_ask` over a brief's live requirements (None without one)."""
    return opening_cut_ask(brief.live()) if brief is not None else None


@dataclass(frozen=True)
class TakeWindowAsk:
    """The lines the take must start at and end right after (either may be None), and the
    brief requirements that asked for each."""

    start_quote: str | None
    end_quote: str | None
    start_ids: tuple[str, ...] = ()
    end_ids: tuple[str, ...] = ()


def take_window_ask(requirements: Iterable[BriefRequirement]) -> TakeWindowAsk | None:
    """The start and end lines among ``requirements``; None when neither is asked."""
    reqs = list(requirements)
    start = _first_line(reqs, requirement_opening_quote)
    end = _first_line(reqs, requirement_end_quote)
    if start is None and end is None:
        return None
    return TakeWindowAsk(
        start_quote=start[0] if start else None,
        end_quote=end[0] if end else None,
        start_ids=start[1] if start else (),
        end_ids=end[1] if end else (),
    )


def brief_take_window(brief: CreativeBrief | None) -> TakeWindowAsk | None:
    """`take_window_ask` over a brief's live requirements (None without one)."""
    return take_window_ask(brief.live()) if brief is not None else None


@dataclass(frozen=True)
class TakeWindowEstimate:
    """Where the quoted lines land in the take before approval (source seconds).

    ``start_s`` / ``end_s`` are None for a line not asked or not found in the timed speech;
    ``kept_s`` (the length the cut keeps) only when every asked line was found.
    """

    start_quote: str | None
    end_quote: str | None
    start_s: float | None
    end_s: float | None
    kept_s: float | None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def segment_words(segments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Timed sentences as words, each timed by its share of its sentence's text.

    Before approval a clip carries sentence timings only; this is the same estimate
    `spoken_quotes.estimate_in_segments` makes, in the word shape the worker's cut reads.
    """
    words: list[dict[str, Any]] = []
    for row in segments:
        start, end = _number(row.get("start_s")), _number(row.get("end_s"))
        text = str(row.get("text") or "")
        if start is None or end is None or end <= start or not text.strip():
            continue
        cursor = 0
        for word in text.split():
            first = text.find(word, cursor)
            cursor = first + len(word)
            words.append(
                {
                    "text": word,
                    "start_s": round(start + (end - start) * first / len(text), 3),
                    "end_s": round(start + (end - start) * cursor / len(text), 3),
                }
            )
    return sorted(words, key=lambda word: word["start_s"])


def _single_take(
    media_snapshot: Mapping[str, Any] | None, strategy: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """The one video clip a Talking edit plays (None for zero or several)."""
    rows = [
        row
        for row in (media_snapshot or {}).get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("kind", "video") == "video"
    ]
    strategy = strategy or {}
    selected = {str(x) for x in strategy.get("selected_media_ids") or []}
    if selected and strategy.get("media_scope") != "all":
        picked = [row for row in rows if str(row.get("media_id") or "") in selected]
        rows = picked or rows
    return rows[0] if len(rows) == 1 else None


@dataclass(frozen=True)
class TakeSpeech:
    """A one-take Talking draft's clip and its timed speech, before approval."""

    media_id: str
    duration_s: float
    # `segment_words` of the clip's timed sentences (source seconds).
    words: tuple[dict[str, Any], ...]


def take_speech(
    media_snapshot: Mapping[str, Any] | None, strategy: Mapping[str, Any] | None = None
) -> TakeSpeech | None:
    """The single take's duration and sentence-timed words; None without them."""
    row = _single_take(media_snapshot, strategy)
    analysis = row.get("analysis") if isinstance(row, Mapping) else None
    if not isinstance(analysis, Mapping):
        return None
    duration = _number(analysis.get("duration_s")) or _number(row.get("duration_s"))
    understanding = analysis.get("understanding")
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    segments = speech.get("segments") if isinstance(speech, Mapping) else None
    words = segment_words(seg for seg in segments or [] if isinstance(seg, Mapping))
    if not duration or not words:
        return None
    return TakeSpeech(str(row.get("media_id") or ""), float(duration), tuple(words))


def estimate_take_window(
    requirements: Iterable[BriefRequirement],
    media_snapshot: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
) -> TakeWindowEstimate | None:
    """The take window a one-take phone Talking draft will be cut to, before approval.

    The worker's own cut (`app.pipeline.phone_opening_cut.apply_take_window`) run on the
    clip's timed sentences. None without a start/end ask; a line the sentences don't hold,
    or a take with no timed speech, stays unknown (never a guessed number).
    """
    ask = take_window_ask(requirements)
    if ask is None:
        return None
    unknown = TakeWindowEstimate(ask.start_quote, ask.end_quote, None, None, None)
    take = take_speech(media_snapshot, strategy)
    if take is None:
        return unknown
    duration, words = take.duration_s, take.words
    from app.pipeline.phone_opening_cut import apply_take_window  # noqa: PLC0415
    from app.pipeline.silence_cut import no_op_plan  # noqa: PLC0415

    plan, start, end = apply_take_window(
        no_op_plan(duration),
        words,
        start_quote=ask.start_quote,
        end_quote=ask.end_quote,
        duration_s=duration,
    )
    receipts = [r for r in (start, end) if r is not None]
    start_s = start.get("start_s") if start and start["status"] == "applied" else None
    end_s = end.get("end_s") if end and end["status"] == "applied" else None
    kept = (
        round(sum(e - s for s, e in plan.keep_segments), 3)
        if all(r["status"] == "applied" for r in receipts)
        else None
    )
    return TakeWindowEstimate(ask.start_quote, ask.end_quote, start_s, end_s, kept)


__all__ = [
    "END_LINE_FACT",
    "OPENING_CUT_KINDS",
    "START_LINE_FACT",
    "OpeningCutAsk",
    "TakeWindowAsk",
    "OccurrencePin",
    "TakeSpeech",
    "TakeWindowEstimate",
    "brief_opening_cut",
    "brief_take_window",
    "estimate_take_window",
    "opening_cut_ask",
    "opening_quote",
    "requirement_end_quote",
    "requirement_opening_quote",
    "answered_occurrence_pins",
    "occurrence_option_key",
    "parse_occurrence_option",
    "segment_words",
    "select_occurrence",
    "take_speech",
    "take_window_ask",
    "unread_line_sides",
    "wants_take_window",
]


# ── KRI-724: which spoken occurrence the creator picked ──────────────────────────────────
#
# "Show MEROPE the first time SHE says Merope": the take's words carry no speaker, so before
# approval the creator is asked which of the times the word is said they mean
# (`choice_questions._speech_occurrence`). The answer is server-owned (a ``speech_occurrence``
# entry in ``choice_answers``, frozen into the ``BriefBinding`` at approval); its option key
# carries the pick, so a model-written fact can never choose an occurrence.

SPEECH_OCCURRENCE_KIND = "speech_occurrence"
OCCURRENCE_WAIVE = "waive"
_OCCURRENCE_KEY = re.compile(  # kri667: parses our own option keys, never creator text
    r"^occ_(?:(\d{1,2})|(all))_of_(\d{1,2})(?:_t(\d{1,6}))?$"
)
# The nearest-time fallback never reaches further than this from the picked time.
_MAX_PIN_DRIFT_S = 1.0


@dataclass(frozen=True)
class OccurrencePin:
    """The creator's pick among the times a word / line is said in the take.

    ``ordinal`` is 1-based among ``of`` occurrences inside the kept take (None = every one);
    ``approx_s`` is where the pick sits in the take (source seconds, from the timed sentences
    the question was built from), for when the render's words count differently.
    """

    ordinal: int | None
    of: int
    approx_s: float | None = None


def occurrence_option_key(ordinal: int | None, of: int, approx_s: float | None = None) -> str:
    """The option key a ``speech_occurrence`` question offers for one pick."""
    head = f"occ_{'all' if ordinal is None else ordinal}_of_{of}"
    if ordinal is None or approx_s is None:
        return head
    return f"{head}_t{round(max(0.0, approx_s) * 100)}"


def parse_occurrence_option(key: object) -> OccurrencePin | None:
    """The pin an option key names; None for a waiver or a malformed key (``occ_9_of_4``)."""
    match = _OCCURRENCE_KEY.match(str(key or ""))
    if match is None:
        return None
    ordinal = int(match.group(1)) if match.group(1) else None
    of = int(match.group(3))
    if of < 1 or (ordinal is not None and not 1 <= ordinal <= of):
        return None
    approx = int(match.group(4)) / 100.0 if match.group(4) else None
    return OccurrencePin(ordinal=ordinal, of=of, approx_s=approx)


def answered_occurrence_pins(
    answers: Iterable[Mapping[str, Any]] | None, requirement_id: str, target: str
) -> OccurrencePin | str | None:
    """The creator's answer for ``target`` (the trigger word / cut line) on ``requirement_id``:
    an `OccurrencePin`, `OCCURRENCE_WAIVE`, or None when no valid answer exists."""
    want = tuple(fold_tokens(target))
    for answer in answers or ():
        if not isinstance(answer, Mapping) or answer.get("kind") != SPEECH_OCCURRENCE_KIND:
            continue
        if requirement_id not in {str(i) for i in answer.get("requirement_ids") or []}:
            continue
        if tuple(fold_tokens(str(answer.get("value") or ""))) != want:
            continue
        option = answer.get("option")
        if option == OCCURRENCE_WAIVE:
            return OCCURRENCE_WAIVE
        return parse_occurrence_option(option)
    return None


def select_occurrence(
    windows: Sequence[tuple[float, float]], pin: OccurrencePin, approx_s: float | None = None
) -> tuple[float, float] | None:
    """The occurrence ``pin`` names among ``windows`` (time-ordered), or None.

    The ordinal decides when the render hears the same number of occurrences the question
    offered; otherwise the occurrence nearest ``approx_s`` (``pin.approx_s`` mapped onto the
    same timeline as ``windows``), only when it is closer than both ``_MAX_PIN_DRIFT_S`` and
    half the gap to its neighbours. Never the first match by default.
    """
    ordered = sorted(windows)
    if not ordered or pin.ordinal is None:
        return None
    if len(ordered) == pin.of:
        return ordered[pin.ordinal - 1]
    target = pin.approx_s if approx_s is None else approx_s
    if target is None:
        return None
    index = min(range(len(ordered)), key=lambda i: abs(ordered[i][0] - target))
    best = ordered[index][0]
    gaps = [
        abs(ordered[j][0] - best) / 2.0 for j in (index - 1, index + 1) if 0 <= j < len(ordered)
    ]
    return ordered[index] if abs(best - target) <= min([_MAX_PIN_DRIFT_S, *gaps]) else None


@dataclass(frozen=True)
class NamedCutAsk:
    """A spoken line the creator named to cut out of the take (KRI-821), and the brief
    requirements that asked for it."""

    quote: str
    requirement_ids: tuple[str, ...]
    # Which time the line is said to cut when the take says it more than once (a retake):
    # 1 = first, 2 = second, ..., -1 = last. None = the creator didn't say which.
    occurrence: int | None = None
    # KRI-724: the creator's answer to "which one?" (`answered_occurrence_pins`), only when
    # `brief_named_cuts` is given the approved answers.
    pin: OccurrencePin | None = None


# KRI-821: "cut the FIRST take of 'welcome to my channel'". The model records which one in
# `facts["cut_line_occurrences"]` ({line: "first" | "second" | "third" | "last"}); it is
# honoured only when the approved requirement text says that ordinal too, so a model guess
# never picks which take of the creator's line is removed.
_OCCURRENCE_WORDS: dict[int, frozenset[str]] = {
    1: frozenset({"first", "1st", "ilk", "birinci"}),
    2: frozenset({"second", "2nd", "ikinci"}),
    3: frozenset({"third", "3rd", "ucuncu"}),
    -1: frozenset({"last", "final", "latter", "sonuncu", "son"}),
}
_OCCURRENCE_VALUES = {"first": 1, "second": 2, "third": 3, "last": -1}


def _named_occurrence(req: BriefRequirement, line: str) -> int | None:
    """The approved ordinal for ``line`` (None when not recorded or not in the text)."""
    raw = req.facts.get("cut_line_occurrences") if isinstance(req.facts, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    key = tuple(fold_tokens(line))
    value = next(
        (v for k, v in raw.items() if isinstance(k, str) and tuple(fold_tokens(k)) == key),
        None,
    )
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        occurrence = _OCCURRENCE_VALUES.get(value.strip().lower())
    elif isinstance(value, int) and value in _OCCURRENCE_WORDS:
        occurrence = value
    else:
        occurrence = None
    if occurrence is None:
        return None
    text_tokens = set(fold_tokens(" ".join(x for x in (req.description, req.literal) if x)))
    return occurrence if text_tokens & _OCCURRENCE_WORDS[occurrence] else None


def _said_in_requirement(req: BriefRequirement, line: str) -> bool:
    """The line's words appear, in order, in the requirement's own text.

    ``cut_lines`` is what the model that read the message recorded; the requirement text is
    what the creator approved. A line the requirement does not carry is never cut.
    """
    line_tokens = fold_tokens(line)
    text_tokens = fold_tokens(" ".join(x for x in (req.description, req.literal) if x))
    width = len(line_tokens)
    return bool(width) and any(
        text_tokens[i : i + width] == line_tokens for i in range(len(text_tokens) - width + 1)
    )


def named_cut_asks(
    requirements: Iterable[BriefRequirement],
    answers: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[NamedCutAsk, ...]:
    """Every live requirement's named cut lines (``facts["cut_lines"]``, KRI-792), each once.

    KRI-724: with the approved ``answers``, a line the creator answered "don't cut it" for is
    left out and a picked occurrence rides on the ask (``pin``)."""
    from app.kria.brief_checks import named_cut_lines  # noqa: PLC0415

    answers = list(answers or ())
    order: list[tuple[str, ...]] = []
    found: dict[tuple[str, ...], tuple[str, list[str]]] = {}
    occurrences: dict[tuple[str, ...], int] = {}
    pins: dict[tuple[str, ...], OccurrencePin] = {}
    for req in requirements:
        if not req.live:
            continue
        for line in named_cut_lines(req):
            key = tuple(fold_tokens(line))
            if not key or not _said_in_requirement(req, line):
                continue
            answered = answered_occurrence_pins(answers, req.id, line) if answers else None
            if answered == OCCURRENCE_WAIVE:
                continue
            if isinstance(answered, OccurrencePin):
                pins.setdefault(key, answered)
            if key not in found:
                order.append(key)
                found[key] = (line, [])
            if req.id not in found[key][1]:
                found[key][1].append(req.id)
            occurrence = _named_occurrence(req, line)
            if occurrence is not None:
                occurrences.setdefault(key, occurrence)
    return tuple(
        NamedCutAsk(
            quote=found[key][0],
            requirement_ids=tuple(found[key][1]),
            occurrence=occurrences.get(key),
            pin=pins.get(key),
        )
        for key in order
    )


def brief_named_cuts(
    brief: CreativeBrief | None, answers: Iterable[Mapping[str, Any]] | None = None
) -> tuple[NamedCutAsk, ...]:
    """`named_cut_asks` over a brief's live requirements (empty without one)."""
    return named_cut_asks(brief.live(), answers) if brief is not None else ()
