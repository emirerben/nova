"""KRI-849: find the words two independent hearings of the same speech disagree on, and keep
the guess out of the captions.

A creator who says "if you can't tell what he says, ask me instead of guessing" needs a signal
that a word is unclear. Production ASR (OpenAI whisper-1) returns no per-word confidence (every
word is 1.0), so the signal is a second, independent hearing of the same speech: Gemini's untimed
transcript in the clip record, against whisper-1's timed words (or, before approval, its timed
sentences). Where they disagree on a stretch of words, that stretch is an "unclear span".

Pure: no I/O, no model, no clock. Used twice and kept identical on purpose:

* before approval (``services/choice_questions._unclear_word``): sentence-timed hearings from the
  approved-media snapshot -> the question "which words did he say?";
* at render (``tasks/generative_build._run_phone_subtitled_job``): the caption words against the
  same Gemini transcript -> :func:`apply_unclear_answers`, which leaves the span out (or writes
  the creator's words) and, when the creator was never answered, leaves it out anyway: the
  captions never print a guess.

Safety rules (each pinned by ``tests/kria/test_kri849_dj_t1_unclear_word_replay.py``):

* Not comparable -> no invented spans (a different language, a missing hearing, too little in
  common, more than :data:`MAX_SPANS` spans). The caller must request a grounded target before
  approval or render; this pure comparison never guesses a window or wipes unrelated captions.
* Noise is not a disagreement: fillers, case, punctuation, accents, digits, near-identical
  spellings, an unmatched run at either edge of the take, and a word the creator listed to spell
  (whisper-1 writes "Creole sauce" for the listed "salsa criolla").
* Receipts and reports never carry the heard words (:meth:`UnclearReport.rows`).

The text here is ASR output, never the creator's message, so word-level comparison is not intent
parsing (KRI-667).
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.text_fold import loose_text

STATE_AGREE = "agree"
STATE_UNCLEAR = "unclear"
STATE_NOT_COMPARABLE = "not_comparable"

ACTION_OMIT = "omit"
ACTION_USE = "use"

# Two words are the same hearing when they are at least this alike (and both have 4+ letters);
# shorter words must match exactly.
FUZZY_RATIO = 0.8
_FUZZY_MIN_LEN = 4
# Of the caption words inside the window at least this share must match the other hearing, else
# the two hearings are not of the same speech (wrong language, different take).
MIN_AGREEMENT = 0.5
# Fewer content words than this inside the window cannot be compared reliably.
MIN_WORDS = 3
# More spans than this = the hearings differ too much to tell which words are unclear.
MAX_SPANS = 2
# An unmatched run with nothing on the other side counts only when it is this long (letters).
_MIN_INSERTED_LETTERS = 6
# The most word pairs one comparison aligns (a very long take is not compared, never slow).
_MAX_CELLS = 1_500_000
# An answer's window matches a span within this many seconds (the answer's window is sentence
# level and in source time; the render span is word level).
MATCH_TOLERANCE_S = 1.5

_FILLERS = frozenset(
    {
        "uh", "um", "umm", "uhm", "uhh", "er", "erm", "eh", "ehm", "ah", "ahh", "hm", "hmm",
        "hmmm", "mm", "mmm", "mhm", "eee", "eeee", "iii", "iiii",
        # Dropped from BOTH hearings, so one hearing writing them and the other not is no span.
        "like", "ok", "okay",
    }
)  # fmt: skip
# Phrases one hearing keeps and the other drops ("so you know we have ..."): removed from both.
_FILLER_PHRASES = (("you", "know"), ("i", "mean"), ("you", "see"))
# English contractions the two hearings write either way ("it's" / "it is"); expanded before
# comparing. The expansion is by apostrophe form, so a plain "its" (possessive) is left alone.
_CONTRACTIONS = {
    "it's": "it is", "that's": "that is", "what's": "what is", "he's": "he is",
    "she's": "she is", "there's": "there is", "here's": "here is", "who's": "who is",
    "where's": "where is", "how's": "how is", "let's": "let us", "i'm": "i am",
    "you're": "you are", "we're": "we are", "they're": "they are", "i've": "i have",
    "you've": "you have", "we've": "we have", "they've": "they have", "i'll": "i will",
    "you'll": "you will", "he'll": "he will", "she'll": "she will", "we'll": "we will",
    "they'll": "they will", "it'll": "it will", "that'll": "that will", "i'd": "i would",
    "you'd": "you would", "he'd": "he would", "she'd": "she would", "we'd": "we would",
    "they'd": "they would", "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "cannot": "can not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "couldn't": "could not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not", "hasn't": "has not", "haven't": "have not",
    "hadn't": "had not", "mustn't": "must not", "gonna": "going to", "wanna": "want to",
    "gotta": "got to", "gimme": "give me", "lemme": "let me", "kinda": "kind of",
    "sorta": "sort of",
}  # fmt: skip
_SENTENCE_END = ".?!…"
_EDGE_CHARS = " \t\n\"'“”‘’«»„()[]{}<>,;:.!?…-–—¿¡"
_SPLIT_CHARS = str.maketrans({"-": " ", "–": " ", "—": " ", "/": " "})
_DROP_CHARS = "'’`´"


# -- tokens ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tok:
    norm: str  # compared form: casefolded, accent-free, letters and digits only
    shown: str  # the word as the hearing wrote it, edge punctuation stripped
    index: int  # position in the hearing's own word list (before filler removal)
    head: bool = True  # False for the 2nd+ token a contraction / hyphenated word is split into


def _norm(word: str) -> str:
    """Letters and digits of ``word`` on the shared fold (``app.text_fold.loose_text``)."""
    return "".join(c for c in loose_text(word) if c.isalnum())


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    for index, raw in enumerate(str(text or "").translate(_SPLIT_CHARS).split()):
        edge = raw.strip(_EDGE_CHARS).replace("\u2019", "'")
        expanded = _CONTRACTIONS.get(edge.casefold())
        if expanded:
            for number, piece in enumerate(expanded.split()):
                shown = raw.strip(_EDGE_CHARS) if number == 0 else piece
                out.append(_Tok(_norm(piece), shown, index, number == 0))
            continue
        norm = _norm(raw.translate({ord(c): None for c in _DROP_CHARS}))
        if norm:
            out.append(_Tok(norm, edge or raw, index))
    return out


def _drop_filler_phrases(tokens: list[Any], norm: Any) -> list[Any]:
    """``tokens`` without the filler phrases ("you know", "I mean"): ``norm(token)`` is the
    compared form."""
    forms = [norm(t) for t in tokens]
    drop: set[int] = set()
    for i in range(len(tokens)):
        for phrase in _FILLER_PHRASES:
            if tuple(forms[i : i + len(phrase)]) == phrase:
                drop.update(range(i, i + len(phrase)))
    return [t for i, t in enumerate(tokens) if i not in drop]


def _is_filler(norm: str) -> bool:
    return norm in _FILLERS


def _has_digit(norm: str) -> bool:
    return any(c.isdigit() for c in norm)


def _alike(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < _FUZZY_MIN_LEN:
        return False
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.real_quick_ratio() < FUZZY_RATIO or matcher.quick_ratio() < FUZZY_RATIO:
        return False
    return matcher.ratio() >= FUZZY_RATIO


# -- timed words ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TimedWord:
    text: str
    start_s: float
    end_s: float


def _timed(words: Iterable[Any]) -> list[TimedWord]:
    out: list[TimedWord] = []
    for word in words or ():
        get = word.get if isinstance(word, Mapping) else lambda k, w=word: getattr(w, k, None)
        text, start, end = get("text"), get("start_s"), get("end_s")
        if (
            not isinstance(text, str)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int | float)
            or not isinstance(end, int | float)
        ):
            continue
        out.append(TimedWord(text, float(start), float(end)))
    return out


def segment_words(segments: Iterable[Mapping[str, Any]]) -> list[TimedWord]:
    """Timed sentences as words, each timed by its share of its sentence's characters (the
    estimate every pre-approval reader of a clip's sentence timings makes).

    Twin of ``app.kria.opening_cut_ask.segment_words`` (same character-share arithmetic, other
    return type); kept as a copy because a pipeline module does not import ``app.kria`` and
    that file belongs to the take-window lane. Change the two together."""
    out: list[TimedWord] = []
    for row in segments or ():
        if not isinstance(row, Mapping):
            continue
        start, end, text = row.get("start_s"), row.get("end_s"), str(row.get("text") or "")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int | float)
            or not isinstance(end, int | float)
            or end <= start
            or not text.strip()
        ):
            continue
        cursor = 0
        for word in text.split():
            first = text.find(word, cursor)
            cursor = first + len(word)
            out.append(
                TimedWord(
                    word,
                    round(start + (end - start) * first / len(text), 3),
                    round(start + (end - start) * cursor / len(text), 3),
                )
            )
    return sorted(out, key=lambda w: w.start_s)


# -- results --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnclearSpan:
    """One stretch of the timed hearing the other hearing heard differently."""

    start_s: float
    end_s: float
    heard: str  # the timed hearing's words (the guess a caption would print)
    other: str  # the other hearing's words for the stretch ("" when it heard nothing)
    first: int  # index of the first / last timed word in the compared word list
    last: int
    key: tuple[str, ...]  # normalised words of both sides: what a question digest depends on
    # False for a window only estimated from sentence timings (pre-approval): captions are cut
    # by the words whose middle falls inside it, not by any overlap.
    precise: bool = True


@dataclass(frozen=True)
class Comparison:
    state: str
    spans: tuple[UnclearSpan, ...] = ()


NOT_COMPARABLE = Comparison(STATE_NOT_COMPARABLE)


# -- the comparison -------------------------------------------------------------------------------


def _spelling_tokens(spellings: Sequence[str]) -> frozenset[str]:
    return frozenset(tok.norm for term in spellings or () for tok in _tokens(term))


def _lcs_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """Matched (a index, b index) pairs of the longest common subsequence under ``_alike``."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    cache: dict[tuple[str, str], bool] = {}

    def same(i: int, j: int) -> bool:
        key = (a[i], b[j])
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = _alike(a[i], b[j])
        return hit

    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, below = table[i], table[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = below[j + 1] + 1 if same(i, j) else max(below[j], row[j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if same(i, j) and table[i][j] == table[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _compare(
    timed: list[TimedWord],
    other_text: str,
    *,
    spellings: Sequence[str],
    window: tuple[float | None, float | None] | None,
) -> Comparison:
    words = [(tok, timed[tok.index]) for tok in _tokens_of(timed)]
    other = _drop_filler_phrases([t for t in _tokens(other_text) if not _is_filler(t.norm)], _form)
    mine = _drop_filler_phrases([(t, w) for t, w in words if not _is_filler(t.norm)], _form_pair)
    if not mine or not other or len(mine) * len(other) > _MAX_CELLS:
        return NOT_COMPARABLE
    lo, hi = window if window else (None, None)

    def inside(word: TimedWord) -> bool:
        mid = (word.start_s + word.end_s) / 2
        return (lo is None or mid >= lo) and (hi is None or mid <= hi)

    pairs = _lcs_pairs([t.norm for t, _ in mine], [t.norm for t in other])
    matched_mine = {i for i, _ in pairs}
    in_window = [i for i, (_, w) in enumerate(mine) if inside(w)]
    if len(in_window) < MIN_WORDS:
        return NOT_COMPARABLE
    if sum(1 for i in in_window if i in matched_mine) / len(in_window) < MIN_AGREEMENT:
        return NOT_COMPARABLE

    listed = _spelling_tokens(spellings)
    spans: list[UnclearSpan] = []
    # Unmatched runs strictly between two matched pairs, plus replaced runs at an edge.
    anchors = [(-1, -1), *pairs, (len(mine), len(other))]
    for (pi, pj), (ni, nj) in zip(anchors, anchors[1:], strict=False):
        run_mine, run_other = mine[pi + 1 : ni], other[pj + 1 : nj]
        if not run_mine:
            continue  # the other hearing heard more: nothing here would be printed
        at_edge = pi == -1 or ni == len(mine)
        if at_edge and not run_other:
            continue  # clipped start, truncated transcript, trailing chatter
        if not any(inside(w) for _, w in run_mine):
            continue
        agreed = (
            mine[pi][0].norm if pi >= 0 else None,
            mine[ni][0].norm if ni < len(mine) else None,
        )
        if _skippable(run_mine, run_other, listed, spellings, agreed):
            continue
        heard = _display([t for t, _ in run_mine])
        said = _display(run_other)
        spans.append(
            UnclearSpan(
                start_s=min(w.start_s for _, w in run_mine),
                end_s=max(w.end_s for _, w in run_mine),
                heard=heard,
                other=said,
                first=run_mine[0][0].index,
                last=run_mine[-1][0].index,
                key=(*(t.norm for t, _ in run_mine), "|", *(t.norm for t in run_other)),
            )
        )
    if len(spans) > MAX_SPANS:
        return NOT_COMPARABLE
    return Comparison(STATE_UNCLEAR, tuple(spans)) if spans else Comparison(STATE_AGREE)


def _form(token: _Tok) -> str:
    return token.norm


def _form_pair(pair: tuple[_Tok, TimedWord]) -> str:
    return pair[0].norm


def _display(tokens: Sequence[_Tok]) -> str:
    """The words of a run as the hearing wrote them (a contraction's pieces read as the one
    word when the run holds the whole of it)."""
    parts: list[str] = []
    previous: _Tok | None = None
    for token in tokens:
        if token.head or previous is None or previous.index != token.index:
            parts.append(token.shown)
        previous = token
    return " ".join(part for part in parts if part)


def _tokens_of(timed: list[TimedWord]) -> list[_Tok]:
    """Tokens of the timed words, ``index`` pointing back into ``timed`` (a contraction or a
    hyphenated word is several tokens of one word)."""
    out: list[_Tok] = []
    for index, word in enumerate(timed):
        shown = word.text.strip(_EDGE_CHARS) or word.text
        for number, piece in enumerate(_tokens(word.text)):
            out.append(_Tok(piece.norm, shown if number == 0 else piece.shown, index, number == 0))
    return out


def _skippable(
    run_mine: Sequence[tuple[_Tok, TimedWord]],
    run_other: Sequence[_Tok],
    listed: frozenset[str],
    spellings: Sequence[str],
    agreed: tuple[str | None, str | None] = (None, None),
) -> bool:
    """A run that is noise or that the creator already told us how to write."""
    mine = [t.norm for t, _ in run_mine]
    theirs = [t.norm for t in run_other]
    if any(a is not None and all(_alike(n, a) for n in mine) for a in agreed):
        return True  # "okay okay okay" against "okay": a repetition of the neighbouring word
    if any(_has_digit(n) for n in (*mine, *theirs)):
        return True  # "15" / "fifteen": the hearings spell numbers differently
    if all(len(n) < 2 for n in mine):
        return True
    if listed and (
        (bool(theirs) and all(n in listed for n in theirs)) or all(n in listed for n in mine)
    ):
        return True  # the creator's own spelling list decides these words
    if spellings and _mishearing_of_listed(run_mine, spellings):
        return True
    if not theirs:
        return sum(len(n) for n in mine) < _MIN_INSERTED_LETTERS
    joined_mine, joined_theirs = "".join(mine), "".join(theirs)
    return _alike(joined_mine, joined_theirs)  # "whats" / "what is", "well known" / "wellknown"


def _mishearing_of_listed(
    run_mine: Sequence[tuple[_Tok, TimedWord]], spellings: Sequence[str]
) -> bool:
    from app.pipeline.caption_spelling import is_mishearing  # noqa: PLC0415

    shown = " ".join(t.shown for t, _ in run_mine)
    return any(is_mishearing(shown, term) for term in spellings)


def compare_words(
    words: Iterable[Any],
    other_text: str | None,
    *,
    spellings: Sequence[str] = (),
    window: tuple[float | None, float | None] | None = None,
) -> Comparison:
    """The words of one hearing (objects or dicts with ``text`` / ``start_s`` / ``end_s``)
    against the other hearing's untimed text."""
    return _compare(_timed(words), str(other_text or ""), spellings=spellings, window=window)


def compare_segments(
    segments: Iterable[Mapping[str, Any]],
    other_text: str | None,
    *,
    spellings: Sequence[str] = (),
    window: tuple[float | None, float | None] | None = None,
) -> Comparison:
    """Timed sentences (``start_s`` / ``end_s`` / ``text``, as a clip record holds them before
    approval) against the other hearing's untimed text; word times are estimated by characters."""
    return _compare(
        segment_words(segments), str(other_text or ""), spellings=spellings, window=window
    )


# -- applying the creator's answers to caption cues -----------------------------------------------


@dataclass(frozen=True)
class UnclearAnswer:
    """What the creator decided for the span at ``start_s``-``end_s`` (the caption timeline):
    ``omit`` it, or ``use`` their own ``text`` instead."""

    action: str
    start_s: float | None = None
    end_s: float | None = None
    text: str = ""


@dataclass(frozen=True)
class AppliedSpan:
    start_s: float
    end_s: float
    action: str  # "omitted" | "written"
    source: str  # "answer" | "model" | "unanswered"
    text: str = ""  # the creator's own words, for "written"


@dataclass(frozen=True)
class UnclearReport:
    """What was done to the captions. Never carries the heard words (a receipt must not print the
    guess); ``text`` is only ever the creator's own."""

    applied: tuple[AppliedSpan, ...] = ()

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "start_s": round(span.start_s, 3),
                "end_s": round(span.end_s, 3),
                "action": span.action,
                "source": span.source,
                **({"text": span.text} if span.text else {}),
            }
            for span in self.applied
        ]


def answer_gap(span: UnclearSpan, answer: UnclearAnswer) -> float | None:
    """How far the answer's window is from the span's centre, ``None`` when it does not reach it
    (within ``MATCH_TOLERANCE_S``) or names no window."""
    if answer.start_s is None or answer.end_s is None:
        return None
    if answer.start_s - MATCH_TOLERANCE_S > span.end_s:
        return None
    if answer.end_s + MATCH_TOLERANCE_S < span.start_s:
        return None
    return abs((answer.start_s + answer.end_s) / 2 - (span.start_s + span.end_s) / 2)


def decide(
    spans: Sequence[UnclearSpan],
    answers: Sequence[UnclearAnswer],
    *,
    ask_active: bool,
    default: UnclearAnswer | None = None,
) -> list[tuple[UnclearSpan, str, str, str]]:
    """``(span, action, text, source)`` for every span the captions must change.

    Each answer settles at most one span (the nearest one its window reaches). A span left without
    an answer takes the model-recorded ``default`` (``omit`` for every open span; typed words only
    when exactly one span is open, else ``omit``: the words cannot be placed), else is left out
    when the ask is active. Without an ask and without answers nothing changes.
    """
    pairs = sorted(
        (gap, position, number)
        for position, span in enumerate(spans)
        for number, answer in enumerate(answers)
        if (gap := answer_gap(span, answer)) is not None
    )
    chosen: dict[int, UnclearAnswer] = {}
    used: set[int] = set()
    for _gap_s, position, number in pairs:
        if position not in chosen and number not in used:
            chosen[position] = answers[number]
            used.add(number)
    open_count = len(spans) - len(chosen)
    decisions: list[tuple[UnclearSpan, str, str, str]] = []
    for position, span in enumerate(spans):
        answer, source = chosen.get(position), "answer"
        if answer is None and default is not None:
            answer, source = default, "model"
            if default.action == ACTION_USE and open_count != 1:
                answer = UnclearAnswer(ACTION_OMIT)
        if answer is not None and answer.action == ACTION_USE and answer.text.strip():
            decisions.append((span, ACTION_USE, answer.text.strip(), source))
        elif answer is not None or ask_active:
            decisions.append(
                (span, ACTION_OMIT, "", source if answer is not None else "unanswered")
            )
    return decisions


def _overlaps(word: Mapping[str, Any], start_s: float, end_s: float) -> bool:
    try:
        return float(word["start_s"]) < end_s - 1e-6 and float(word["end_s"]) > start_s + 1e-6
    except (KeyError, TypeError, ValueError):
        return False


def _middle_in(word: Mapping[str, Any], start_s: float, end_s: float) -> bool:
    """The word's middle lies inside the window (for a window only estimated from sentence
    timings, whose edges are off by a word: the neighbour that merely touches it stays)."""
    try:
        middle = (float(word["start_s"]) + float(word["end_s"])) / 2
    except (KeyError, TypeError, ValueError):
        return False
    return start_s - 1e-6 <= middle <= end_s + 1e-6


def drop_windows(
    cues: Sequence[dict[str, Any]], windows: Sequence[tuple[float, float]]
) -> list[dict[str, Any]]:
    """The minimal path: ``cues`` without the words whose middle falls in any window, text
    rebuilt from what remains, emptied cues dropped, nothing retimed. No decisions, no comparison:
    what the writer falls back to when the main step failed."""
    out: list[dict[str, Any]] = []
    for cue in cues:
        words = cue.get("words")
        if not isinstance(words, list):
            out.append(dict(cue))
            continue
        kept = [w for w in words if not any(_middle_in(w, lo, hi) for lo, hi in windows)]
        if len(kept) == len(words):
            out.append(dict(cue))
            continue
        text = " ".join(str(w.get("text") or "") for w in kept).strip()
        if text:
            out.append({**cue, "words": kept, "text": text})
    return out


def _replacement(words: Sequence[Mapping[str, Any]], text: str) -> list[dict[str, Any]]:
    """The creator's ``text`` as words spread over the window ``words`` held, keeping the closing
    punctuation the heard last word carried."""
    start_s = float(words[0]["start_s"])
    end_s = float(words[-1]["end_s"])
    last = str(words[-1].get("text") or "").rstrip()[-1:]
    closing = last if last in _SENTENCE_END else ""
    pieces = text.split()
    step = (end_s - start_s) / len(pieces)
    out: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces):
        if i == len(pieces) - 1 and closing and piece[-1:] not in _SENTENCE_END:
            piece += closing
        out.append(
            {
                **words[0],
                "text": piece,
                "start_s": round(start_s + step * i, 3),
                "end_s": round(start_s + step * (i + 1), 3),
            }
        )
    return out


def apply_unclear_answers(
    cues: Sequence[dict[str, Any]],
    spans: Sequence[UnclearSpan],
    answers: Sequence[UnclearAnswer] = (),
    *,
    ask_active: bool,
    default: UnclearAnswer | None = None,
) -> tuple[list[dict[str, Any]], UnclearReport]:
    """Caption ``cues`` with each unclear span left out, or replaced by the creator's own words.

    Timings never move: a cue keeps its ``start_s`` / ``end_s``, every other word its own times,
    and a cue left with no words is dropped. ``text`` is rebuilt from the words that remain, so
    text and words stay in step. With ``ask_active`` a span nobody answered is left out too: the
    captions never print a guess.
    """
    decisions = decide(spans, answers, ask_active=ask_active, default=default)
    if not decisions:
        return list(cues), UnclearReport()
    out = [dict(cue) for cue in cues]
    applied: list[AppliedSpan] = []
    for span, action, text, source in decisions:
        placed = False
        for cue in out:
            words = cue.get("words")
            if not isinstance(words, list):
                # No word times to cut by: only a cue lying wholly inside the span can go.
                if (
                    _overlaps(cue, span.start_s, span.end_s)
                    and float(cue["start_s"]) >= span.start_s - 1e-6
                    and float(cue["end_s"]) <= span.end_s + 1e-6
                ):
                    cue["text"] = ""
                continue
            inside = _overlaps if span.precise else _middle_in
            hit = [i for i, word in enumerate(words) if inside(word, span.start_s, span.end_s)]
            if not hit:
                continue
            first, last = hit[0], hit[-1]
            put = (
                _replacement(words[first : last + 1], text)
                if action == ACTION_USE and not placed
                else []
            )
            placed = placed or bool(put)
            cue["words"] = [*words[:first], *put, *words[last + 1 :]]
            cue["text"] = " ".join(str(w.get("text") or "") for w in cue["words"]).strip()
        applied.append(
            AppliedSpan(
                span.start_s,
                span.end_s,
                "written" if action == ACTION_USE else "omitted",
                source,
                text if action == ACTION_USE else "",
            )
        )
    return [cue for cue in out if str(cue.get("text") or "").strip()], UnclearReport(tuple(applied))


def format_time(seconds: float) -> str:
    """``m:ss`` for a question or a receipt ("0:09")."""
    whole = max(0, round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


__all__ = [
    "ACTION_OMIT",
    "ACTION_USE",
    "MAX_SPANS",
    "NOT_COMPARABLE",
    "STATE_AGREE",
    "STATE_NOT_COMPARABLE",
    "STATE_UNCLEAR",
    "AppliedSpan",
    "Comparison",
    "TimedWord",
    "UnclearAnswer",
    "UnclearReport",
    "UnclearSpan",
    "answer_gap",
    "apply_unclear_answers",
    "drop_windows",
    "compare_segments",
    "compare_words",
    "decide",
    "format_time",
    "segment_words",
]
