"""Which sentence of the creator's talk a clip is about, and where the take pauses for it.

"Place them in between my speech": the take is PAUSED for each clip, at the END OF THE SENTENCE
(or complete thought) in which the creator talks about what the clip shows. Not at the matched
word, not mid-sentence, not at the nearest caption end. Pure (no I/O, no settings, no model):

1. `split_sentences` cuts his spoken words into sentences: Whisper's terminal punctuation, a
   long pause where it wrote none, and a hard cap on length so a run with no punctuation and no
   pause is still cut at its longest gap, never inside a word.
2. `resolve_topics` turns the meaning check's answers (`TopicPick`, one per clip, see
   `app.agents.talking_insert_sentence`) into a pause point per clip. Meaning decides because the
   words a clip is described in need not be the words he says (English facts, Turkish speech):
   the keyword matcher (`phone_talking_mentions.keyword_mentions`) is only a fallback when the
   check could not run, and the tie-breaker that confirms a low-confidence answer. A model that
   says "no sentence is about this" is believed: a keyword never overrides it.

The sentence end is a DEFAULT position, used because the creator named none. Two defaults can
move it, and the resolution says so (``adjusted``): a sentence that ends in the first seconds
(`HOOK_FREE_S`) is paused after the next one that does not, and the last sentence of the talk is
paused just before. An explicit phrase or second is never touched here.

Every outcome carries a ``reason`` code the receipt and the reply word for the creator; nothing
is dropped silently and nothing is guessed without being labelled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.phone_talking_cutaways import HOOK_FREE_S, TAIL_FREE_S

__all__ = [
    "CONFIDENCES",
    "MAX_SENTENCE_WORDS",
    "MAX_TOPIC_SENTENCES",
    "NO_MATCHING_SENTENCE",
    "LOW_CONFIDENCE",
    "SENTENCE_PAUSE_S",
    "TOPIC_CHECK_UNAVAILABLE",
    "Sentence",
    "TopicPick",
    "TopicResolution",
    "resolve_topics",
    "split_sentences",
]

# A silence this long ends a sentence even where Whisper wrote no punctuation.
SENTENCE_PAUSE_S = 0.8
# A run longer than this is cut at its longest internal gap.
MAX_SENTENCE_WORDS = 40
# A thought that runs across more sentences than this is not "a sentence about the clip".
MAX_TOPIC_SENTENCES = 3
CONFIDENCES = ("high", "medium", "low")

NO_MATCHING_SENTENCE = "no_matching_sentence"
LOW_CONFIDENCE = "low_confidence"
TOPIC_CHECK_UNAVAILABLE = "topic_check_unavailable"

_TERMINAL = ".?!…"
_CLOSERS = "\"')]}»”’"
_EPS = 1e-6


@dataclass(frozen=True)
class Sentence:
    """One sentence of his talk on the take timeline; ``index`` is 1-based."""

    index: int
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class TopicPick:
    """The meaning check's answer for one clip: the sentences (``first``..``last``, 1-based,
    inclusive) that carry what the clip shows, or ``None``/``None`` when none does."""

    media_id: str
    first: int | None
    last: int | None
    confidence: str = "high"


@dataclass(frozen=True)
class TopicResolution:
    """Where one clip's pause goes, or why it has none. ``via`` is "meaning" (the check's
    answer) or "keyword" (the fallback)."""

    media_id: str
    point_s: float | None = None
    first: int | None = None
    last: int | None = None
    via: str | None = None
    confidence: str | None = None
    reason: str | None = None
    # "hook_window" | "tail_window": why the point is not the end of sentence ``last``.
    adjusted: str | None = None


def _get(word: Any, name: str, default: Any = None) -> Any:
    return word.get(name, default) if isinstance(word, Mapping) else getattr(word, name, default)


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip(_CLOSERS)
    return bool(stripped) and stripped[-1] in _TERMINAL


def _cut_long(run: list[dict]) -> list[list[dict]]:
    """``run`` split at its longest gap until no piece exceeds `MAX_SENTENCE_WORDS` words."""
    if len(run) <= MAX_SENTENCE_WORDS:
        return [run]
    lo = max(1, len(run) - MAX_SENTENCE_WORDS)
    hi = min(len(run) - 1, MAX_SENTENCE_WORDS)
    # A cut must leave neither side over the cap when it can; pick the widest gap among those.
    candidates = range(lo, hi + 1) if lo <= hi else range(1, len(run))
    cut = max(
        candidates, key=lambda i: (run[i]["start_s"] - run[i - 1]["end_s"], -abs(i - len(run) / 2))
    )
    return _cut_long(run[:cut]) + _cut_long(run[cut:])


def split_sentences(words: Sequence[Any], take_s: float) -> list[Sentence]:
    """The creator's spoken ``words`` (take timeline, time ordered) as numbered sentences.

    A word ends a sentence when its text ends with terminal punctuation (a decimal such as
    "70.000" does not: the dot is inside the word) or when the silence after it is at least
    `SENTENCE_PAUSE_S`. Words that start after ``take_s`` are not part of the take.
    """
    rows = sorted(
        (
            {
                "text": str(_get(w, "text", "") or "").strip(),
                "start_s": float(_get(w, "start_s", 0.0) or 0.0),
                "end_s": float(_get(w, "end_s", 0.0) or 0.0),
            }
            for w in words
        ),
        key=lambda r: r["start_s"],
    )
    rows = [r for r in rows if r["text"] and r["start_s"] < take_s - _EPS]
    runs: list[list[dict]] = []
    current: list[dict] = []
    for index, row in enumerate(rows):
        current.append(row)
        following = rows[index + 1]["start_s"] if index + 1 < len(rows) else None
        if (
            _ends_sentence(row["text"])
            or following is None
            or following - row["end_s"] >= SENTENCE_PAUSE_S
        ):
            runs.append(current)
            current = []
    sentences: list[Sentence] = []
    for run in runs:
        for piece in _cut_long(run):
            sentences.append(
                Sentence(
                    index=len(sentences) + 1,
                    text=" ".join(r["text"] for r in piece),
                    start_s=round(piece[0]["start_s"], 3),
                    end_s=round(piece[-1]["end_s"], 3),
                )
            )
    return sentences


def _sentence_at(sentences: Sequence[Sentence], t: float) -> Sentence | None:
    """The sentence ``t`` falls in (a moment between two sentences belongs to the one before)."""
    found: Sentence | None = None
    for sentence in sentences:
        if sentence.start_s - _EPS <= t:
            found = sentence
        else:
            break
    return found


def _at_sentence(
    sentences: Sequence[Sentence], first: int, last: int, take_s: float
) -> tuple[float, str | None]:
    """The pause point for the thought ``first``..``last`` and what moved it off that sentence's
    end: the first seconds stay on him (pause after the next sentence that ends later), and the
    last sentence of the talk has nothing after it (pause just before it)."""
    point = sentences[last - 1].end_s
    if point < HOOK_FREE_S - _EPS:
        later = next((s for s in sentences[last:] if s.end_s >= HOOK_FREE_S - _EPS), None)
        if later is not None and later.end_s <= take_s - TAIL_FREE_S + _EPS:
            return later.end_s, "hook_window"
    if point > take_s - TAIL_FREE_S + _EPS and last >= 2:
        before = sentences[last - 2].end_s
        if before >= HOOK_FREE_S - _EPS:
            return before, "tail_window"
    return point, None


def _keyword_resolution(
    media_id: str,
    sentences: Sequence[Sentence],
    keyword_end_s: Mapping[str, float],
    take_s: float,
) -> TopicResolution | None:
    end = keyword_end_s.get(media_id)
    if end is None:
        return None
    sentence = _sentence_at(sentences, end - _EPS)
    if sentence is None:
        return None
    point, adjusted = _at_sentence(sentences, sentence.index, sentence.index, take_s)
    return TopicResolution(
        media_id,
        point_s=point,
        first=sentence.index,
        last=sentence.index,
        via="keyword",
        adjusted=adjusted,
    )


def _valid(pick: TopicPick, count: int) -> bool:
    return (
        pick.first is not None
        and pick.last is not None
        and 1 <= pick.first <= pick.last <= count
        and pick.last - pick.first + 1 <= MAX_TOPIC_SENTENCES
    )


def resolve_topics(
    media_ids: Sequence[str],
    sentences: Sequence[Sentence],
    *,
    picks: Mapping[str, TopicPick] | None,
    keyword_end_s: Mapping[str, float],
    take_s: float,
) -> list[TopicResolution]:
    """One `TopicResolution` per clip, in the order asked.

    ``picks`` is the meaning check's answer, or ``None`` when it could not run (then keywords
    decide and the row says ``via="keyword"``). ``keyword_end_s`` maps a clip to the end of the
    last word of its best keyword mention, for the clips that have one.
    """
    out: list[TopicResolution] = []
    for media_id in media_ids:
        if picks is None:
            found = _keyword_resolution(media_id, sentences, keyword_end_s, take_s)
            out.append(found or TopicResolution(media_id, reason=TOPIC_CHECK_UNAVAILABLE))
            continue
        pick = picks.get(media_id)
        if pick is None or not _valid(pick, len(sentences)):
            out.append(TopicResolution(media_id, reason=NO_MATCHING_SENTENCE))
            continue
        assert pick.first is not None and pick.last is not None
        if pick.confidence == "low":
            keyword = _keyword_resolution(media_id, sentences, keyword_end_s, take_s)
            if keyword is None or not pick.first <= (keyword.last or 0) <= pick.last:
                out.append(TopicResolution(media_id, reason=LOW_CONFIDENCE))
                continue
        point, adjusted = _at_sentence(sentences, pick.first, pick.last, take_s)
        out.append(
            TopicResolution(
                media_id,
                point_s=point,
                first=pick.first,
                last=pick.last,
                via="meaning",
                confidence=pick.confidence,
                adjusted=adjusted,
            )
        )
    return out
