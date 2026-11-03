"""Which sentence of the creator's talk a clip is about, and where the take pauses for it.

"Place them in between my speech": the take is PAUSED for each clip, at the END OF THE SENTENCE
(or complete thought) in which the creator talks about what the clip shows. Not at the matched
word, not mid-sentence, not at the nearest caption end. Pure (no I/O, no settings, no model):

1. `split_sentences` cuts his spoken words into sentences: Whisper's terminal punctuation, a
   long pause where it wrote none, and a hard cap on length so a run with no punctuation and no
   pause is still cut at its longest gap, never inside a word.
2. `resolve_topics` turns the meaning check's answers (`TopicPick`, one per clip, see
   `app.agents.talking_insert_sentence`) into a pause point per clip. Meaning decides because the
   words a clip is described in need not be the words he says (English facts, Turkish speech).

A clip is left out in exactly two cases: there is no speech to place it after, or the meaning
check said, with high confidence, that nothing he says is about it (the creator is told which
clip and offered the nearest sentence). Everything else places the clip, and the resolution says
how (``via``) and why it is not the plain meaning answer (``why``): the keyword matcher
(`phone_talking_mentions.keyword_mentions`) when the check gave no usable answer for the clip,
else a labelled guess (``via="guess"``): the sentence the check gave with low confidence, the
sentence sharing the most words with the clip, or an even spread across his talk when nothing
shares a word. A default can never block the creator's own request.

The sentence end is a DEFAULT position, used because the creator named none. One default can
move it, and the resolution says so (``adjusted``): a sentence that ends in the first seconds
(`HOOK_FREE_S`) is paused after the next one that does not. The last sentence of the talk is
paused AFTER it (the compiler plays a clip at the very end of the take). An explicit phrase or
second is never touched here.

Every outcome carries a code the receipt and the reply word for the creator; nothing is dropped
silently and nothing is guessed without being labelled.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.pipeline.phone_talking_cutaways import HOOK_FREE_S
from app.pipeline.phone_talking_mentions import match_keys

__all__ = [
    "CONFIDENCES",
    "MAX_SENTENCE_WORDS",
    "NO_MATCHING_SENTENCE",
    "NO_SPEECH",
    "SENTENCE_PAUSE_S",
    "WHY_CHECK_UNAVAILABLE",
    "WHY_INVALID_ANSWER",
    "WHY_LOW_CONFIDENCE",
    "WHY_NO_FACTS",
    "WHY_UNCLEAR",
    "Sentence",
    "TopicPick",
    "TopicResolution",
    "needs_retry",
    "resolve_topics",
    "split_sentences",
]

# A silence this long ends a sentence even where Whisper wrote no punctuation.
SENTENCE_PAUSE_S = 0.8
# A run longer than this is cut at its longest internal gap.
MAX_SENTENCE_WORDS = 40
CONFIDENCES = ("high", "medium", "low")

# The only two reasons a clip is left out (``TopicResolution.reason``).
NO_MATCHING_SENTENCE = "no_matching_sentence"  # the check said, surely, nothing is about it
NO_SPEECH = "no_speech"  # nothing was said to place it after
# Why a placed clip is not the plain meaning answer (``TopicResolution.why``).
WHY_CHECK_UNAVAILABLE = "check_unavailable"  # the check did not run or gave nothing at all
WHY_UNCLEAR = "unclear"  # the check could not tell which sentence (a null that is not sure)
WHY_INVALID_ANSWER = "invalid_answer"  # no answer for this clip, or numbers that are no sentence
WHY_LOW_CONFIDENCE = "low_confidence"  # the check was guessing and no word confirms it
WHY_NO_FACTS = "no_facts"  # nothing is known about the clip to match

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
    answer), "keyword" (the words matched) or "guess" (a labelled best fit); ``why`` is set for
    the last two. ``nearest`` is, for a clip left out, the 1-based sentence that fits it best
    (the creator is offered it), when any fits better than another."""

    media_id: str
    point_s: float | None = None
    first: int | None = None
    last: int | None = None
    via: str | None = None
    confidence: str | None = None
    reason: str | None = None
    why: str | None = None
    # "hook_window": why the point is not the end of sentence ``last``.
    adjusted: str | None = None
    nearest: int | None = None


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
    end: the first seconds stay on him (pause after the next sentence that ends later). The end
    of the last sentence is the end of the take: the pause goes there."""
    point = sentences[last - 1].end_s
    if point < HOOK_FREE_S - _EPS:
        later = next((s for s in sentences[last:] if s.end_s >= HOOK_FREE_S - _EPS), None)
        if later is not None:
            return later.end_s, "hook_window"
    return min(point, take_s), None


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


def _clamped(pick: TopicPick, count: int) -> tuple[int, int] | None:
    """The pick's sentences as a valid 1-based range, or ``None`` when it names none. Numbers
    outside the talk are moved to its nearest sentence and a reversed range is turned round,
    because the model meant a stretch of his talk; the answer is only unusable when it names no
    number at all."""
    if pick.first is None or pick.last is None:
        return None
    first, last = sorted((int(pick.first), int(pick.last)))
    first = min(max(first, 1), count)
    last = min(max(last, 1), count)
    return first, last


def needs_retry(pick: TopicPick | None, count: int) -> bool:
    """Whether the check's answer for a clip is worth asking once more: none at all, numbers
    that name no sentence, a null that is not sure, or a guess. Only a placed answer the model
    stands behind, or a surely-nothing null, is final."""
    if pick is None:
        return True
    if pick.first is None and pick.last is None:
        return pick.confidence != "high"
    if _clamped(pick, count) is None:
        return True
    return pick.confidence == "low"


def _scores(sentences: Sequence[Sentence], keys: frozenset[str] | None) -> list[int]:
    """How many of the clip's words each sentence shares (all zero when nothing is known)."""
    if not keys:
        return [0] * len(sentences)
    return [len(keys & frozenset(match_keys(sentence.text))) for sentence in sentences]


def _best(scores: Sequence[int]) -> int | None:
    """The 1-based sentence with the most shared words, the later one on a tie; ``None`` when no
    sentence shares any."""
    top = max(scores, default=0)
    if top <= 0:
        return None
    return max(i for i, score in enumerate(scores, start=1) if score == top)


def resolve_topics(
    media_ids: Sequence[str],
    sentences: Sequence[Sentence],
    *,
    picks: Mapping[str, TopicPick] | None,
    keyword_end_s: Mapping[str, float],
    take_s: float,
    clip_keys: Mapping[str, frozenset[str]] | None = None,
    no_facts: Collection[str] = (),
) -> list[TopicResolution]:
    """One `TopicResolution` per clip, in the order asked.

    ``picks`` is the meaning check's answer, or ``None`` when it could not run (then keywords
    decide, else a labelled guess). ``keyword_end_s`` maps a clip to the end of the last word of
    its best keyword mention, for the clips that have one. ``clip_keys`` are the words each clip
    is about (they score a guess); ``no_facts`` are clips nothing is known about.
    """
    if not sentences:
        return [TopicResolution(media_id, reason=NO_SPEECH) for media_id in media_ids]
    count = len(sentences)
    keys = clip_keys or {}
    out: dict[str, TopicResolution] = {}
    unscored: list[tuple[str, str]] = []  # guesses nothing helps: spread over the talk after

    def place(media_id: str, first: int, last: int, via: str, **kw: Any) -> None:
        point, adjusted = _at_sentence(sentences, first, last, take_s)
        out[media_id] = TopicResolution(
            media_id,
            point_s=point,
            first=first,
            last=last,
            via=via,
            adjusted=adjusted,
            **kw,
        )

    def fall_back(media_id: str, why: str, given: tuple[int, int] | None = None) -> None:
        keyword = _keyword_resolution(media_id, sentences, keyword_end_s, take_s)
        if keyword is not None:
            out[media_id] = replace(keyword, why=why)
            return
        if given is not None:
            place(media_id, given[0], given[1], "guess", why=why)
            return
        best = _best(_scores(sentences, keys.get(media_id)))
        if best is None:
            unscored.append((media_id, why))
            return
        place(media_id, best, best, "guess", why=why)

    for media_id in media_ids:
        if media_id in no_facts:
            fall_back(media_id, WHY_NO_FACTS)
            continue
        if picks is None:
            fall_back(media_id, WHY_CHECK_UNAVAILABLE)
            continue
        pick = picks.get(media_id)
        if pick is None:
            fall_back(media_id, WHY_INVALID_ANSWER)
            continue
        span = _clamped(pick, count)
        if span is None:
            if pick.first is None and pick.last is None:
                if pick.confidence == "high":
                    # The one answer that leaves a clip out: surely nothing is about it.
                    out[media_id] = TopicResolution(
                        media_id,
                        reason=NO_MATCHING_SENTENCE,
                        nearest=_best(_scores(sentences, keys.get(media_id))),
                    )
                else:
                    fall_back(media_id, WHY_UNCLEAR)
            else:
                fall_back(media_id, WHY_INVALID_ANSWER)
            continue
        if pick.confidence == "low":
            keyword = _keyword_resolution(media_id, sentences, keyword_end_s, take_s)
            if keyword is None or not span[0] <= (keyword.last or 0) <= span[1]:
                fall_back(media_id, WHY_LOW_CONFIDENCE, given=span)
                continue
        place(media_id, span[0], span[1], "meaning", confidence=pick.confidence)
    for index, (media_id, why) in enumerate(unscored, start=1):
        spot = min(count, max(1, round(index * count / (len(unscored) + 1))))
        place(media_id, spot, spot, "guess", why=why)
    return [out[media_id] for media_id in media_ids]
