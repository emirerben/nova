"""Creator spelling asks for caption words (KRI-631).

"Write 'community' in English" / "spell 'Deniz' and 'Airbnb' correctly": whisper hears
the word and writes it the way it sounds in the spoken language ("komüniti",
"komünitenin"). Two pure steps, no model call:

- ``extract_spelling_asks`` reads the quoted words the creator wants spelled as written,
  from the creator's own brief text. A quoted single word counts only in a clause that
  is about captions or spelling, so quoted on-screen text ("title: "Hello"") is ignored.
- ``apply_spelling_to_cues`` rewrites caption words that sound like one of those words,
  keeping a Turkish case ending after an apostrophe ("komünitenin" -> "community'nin").

The match is deliberately narrow (a wrong replacement is worse than a missed one): same
first sound, same last sound class, at most 1-2 sound-folded edits, never a word that
already reads as the target, and no fuzzy matching for short targets.

KRI-793: a target may be several words ("Oblea de la Casa", the creator's own list in the
approved brief). Such a phrase is matched as adjacent words with nothing but spaces between
them; the same sound rule then runs on the words joined ("Ole de la Casa" -> "Oblea de la
Casa"), so every word keeps its own timing.

KRI-961: whisper can also hear a phrase as one word fewer or more ("ojo de bife" heard as
"Olloda bife"). ``find_named_phrase`` is the one matcher for a phrase the creator named,
shared by the captions (respelled here) and the reaction-beat grounding (the card shows when
the voice says it, `app.services.phone_reaction_grounding`). A span with another word count
counts only with a strong signal: one word more or fewer, at least one word of the phrase
(3+ letters) heard exactly, and the joined words within the same sound rule -- or the same
letters with other spacing ("Kızkulesi" for "Kız Kulesi"). The words heard exactly always
win, and a one-word phrase only ever matches one heard word. When the count changes, a
heard word split in two shares its time by letters and two heard words merged keep both
times.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.text_fold import loose_case

MAX_ASKS = 12
_MIN_TARGET_LEN = 5


def _fold(text: str) -> str:
    """Lowercase and strip Turkish diacritics; "İ"/"I" both become "i".

    The shared Turkish-aware fold (KRI-781), whitespace kept as written.
    """
    return loose_case(text or "")


# Quoted single words only: straight, curly and guillemet double quotes.
_QUOTED = re.compile(r'"([^"\n]{1,40})"|“([^”\n]{1,40})”|„([^“”\n]{1,40})[“”]|«([^»\n]{1,40})»')
_SINGLE_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)?")
_CLAUSE_SPLIT = re.compile(r"[;\n!?]+|\.(?=\s|$)")
_CAPTION_WORD = re.compile(r"\b(?:captions?|subtitles?|subtitled|karaoke|altyazi\w*)\b")
# Wordings that say "spell/write this word as I give it", in folded text.
_SPELL_CUE = re.compile(
    r"\b(?:spell(?:ed|ing|s)?|spelt|written|"
    r"(?:ingilizce|english|turkce|turkish)\w*\s+(?:yaz\w*|olsun)|"
    r"(?:yaz\w*|write)\s+(?:\w+\s+){0,3}?(?:in\s+)?(?:english|ingilizce\w*)|"
    r"dogru\s+yaz\w*|yazim\w*|imla\w*|hatasiz\s+yaz\w*)\b"
)
_NEGATION = re.compile(r"\b(?:don'?t|do\s+not|never|without|istemiyorum|olmasin\w*|yazma)\b")


def extract_spelling_asks(text: str | None) -> tuple[str, ...]:
    """The quoted words the creator wants spelled exactly as written, in order, unique."""
    asks: list[str] = []
    for clause in _CLAUSE_SPLIT.split(text or ""):
        folded = _fold(clause)
        if _NEGATION.search(folded):
            continue
        if not (_CAPTION_WORD.search(folded) or _SPELL_CUE.search(folded)):
            continue
        for found in _QUOTED.finditer(clause):
            word = next(g for g in found.groups() if g is not None).strip()
            if _SINGLE_WORD.fullmatch(word) and len(word) >= _MIN_TARGET_LEN and word not in asks:
                asks.append(word)
    return tuple(asks[:MAX_ASKS])


_SOUND_FOLD = str.maketrans({"y": "i", "c": "k", "q": "k", "w": "v"})
_VOWELS = frozenset("aeiou")
# Case/possessive endings a Turkish speaker glues onto a loanword ("komünitenin"), folded.
_SUFFIXES = frozenset(
    "n nin nun in un i u e a ni nu yi yu ye ya de da te ta den dan ten tan nde nda nden ndan "
    "le la ile ler lar leri lari lerin larin lerde larda si su im imiz iniz".split()
)
# Whisper often drops the vowel of "-nin" after a vowel-final loanword ("komünitin").
_TRUNCATED_GENITIVE = {"n": "nin"}


def _sound(word: str) -> str:
    """Folded, sound-approximate key: "community" and "komüniti" are both "komuniti"."""
    key = re.sub(r"[^a-z]", "", _fold(word).translate(_SOUND_FOLD))
    return re.sub(r"(.)\1+", r"\1", key)


def _edit_distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _max_edits(sound: str) -> int:
    return 0 if len(sound) <= 5 else 1 if len(sound) <= 8 else 2


def _sounds_like(stem: str, target: str) -> bool:
    """True when ``stem`` is a mis-hearing of ``target`` but not already ``target``."""
    if len(stem) < _MIN_TARGET_LEN - 1 or _fold(stem) == _fold(target):
        return False
    want, got = _sound(target), _sound(stem)
    if not want or not got or want[0] != got[0]:
        return False
    # The last sound must be the same, or a vowel on both sides: "komünist" is not
    # "community", "komünite" is.
    if want[-1] != got[-1] and not (want[-1] in _VOWELS and got[-1] in _VOWELS):
        return False
    return _edit_distance(want, got, _max_edits(want)) <= _max_edits(want)


def _match_word(word: str, target: str) -> str | None:
    """``word`` respelled as ``target`` (with its ending kept), or None to leave it.

    A target shorter than ``_MIN_TARGET_LEN`` is never matched: "Casa" would take Turkish
    "kasada" (KRI-793 brief lists can hold short words).
    """
    if len(_fold(target)) < _MIN_TARGET_LEN:
        return None
    stem_part, apostrophe, tail = re.match(r"([^'’]*)(['’]?)(.*)", word).groups()  # type: ignore[union-attr]
    if apostrophe:
        # "komünite'nin": the creator-visible ending is already split off.
        if not _sounds_like(stem_part, target):
            return None
        return _recase(word[0], target) + apostrophe + tail
    low = _fold(word)
    if low == _fold(target):
        return None
    # The name spelled right with a Turkish ending glued on ("Meropeye"): only the
    # apostrophe is missing, so no shorter misheard stem may claim it ("Merope'e").
    for cut in range(len(word) - 1, _MIN_TARGET_LEN - 2, -1):
        ending = low[cut:]
        if ending in _SUFFIXES and _fold(word[:cut]) == _fold(target):
            return _recase(word[0], target) + "'" + _TRUNCATED_GENITIVE.get(ending, word[cut:])
    # Longest stem first, with a Turkish ending, so "komünitenin" keeps its "nin".
    for cut in range(len(word) - 1, _MIN_TARGET_LEN - 2, -1):
        ending = low[cut:]
        if ending in _SUFFIXES and _sounds_like(word[:cut], target):
            shown = _TRUNCATED_GENITIVE.get(ending, word[cut:])
            return _recase(word[0], target) + "'" + shown
    if _sounds_like(word, target):
        return _recase(word[0], target)
    return None


def _recase(first: str, target: str) -> str:
    return target[:1].upper() + target[1:] if first.isupper() and target[:1].islower() else target


_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")


def _phrase_words(target: str) -> list[str]:
    return target.split()


def _match_phrase(words: Sequence[str], target: str) -> list[str] | None:
    """``words`` respelled as the words of the phrase ``target``, or None to leave them.

    Same word count, not already the target, and the joined words sound like the joined
    target under the single-word rule (first sound, last sound class, 1-2 edits).
    """
    wanted = _phrase_words(target)
    if len(words) != len(wanted) or len(wanted) < 2:
        return None
    if any(_APOSTROPHE.search(word) for word in words[:-1]):
        return None  # a case ending closes a name ("Casa'da" is not mid-phrase)
    # A Turkish ending on the last word stays ("Ole de la Casa'da" -> "Oblea de la Casa'da").
    last, apostrophe, ending = _APOSTROPHE_SPLIT.match(words[-1]).groups()  # type: ignore[union-attr]
    stems = [*words[:-1], last]
    if [_fold(word) for word in stems] == [_fold(word) for word in wanted]:
        return None
    if not _sounds_like("".join(stems), "".join(wanted)):
        return None
    out = [_recase(words[0][0], wanted[0]), *wanted[1:]]
    out[-1] += apostrophe + ending
    return out


_APOSTROPHE = re.compile(r"['’]")
_APOSTROPHE_SPLIT = re.compile(r"([^'’]*)(['’]?)(.*)", re.DOTALL)
_SPACES_ONLY = re.compile(r"\s+")

# --- KRI-961: the creator-named phrase matcher -------------------------------------------

# A span with one word more or fewer than the phrase must hear one of the phrase's words of
# at least this many letters exactly (folded): "Olloda bife" hears "bife". A short function
# word ("de", "la") never anchors a fuzzy match on its own.
_ANCHOR_MIN_LETTERS = 3
_SENTENCE_ENDS = (".", "!", "?", "…")
_OPENING_MARKS = "\"'“”‘’«»(["


@dataclass(frozen=True)
class NamedPhraseHit:
    """``words[start:stop]`` say the phrase. ``exact``: they already read as it (folded, a
    case ending on the last word allowed), so there is nothing to respell."""

    start: int
    stop: int
    exact: bool


def _split_ending(word: str) -> tuple[str, str]:
    """``word`` and the Turkish case ending after its apostrophe ("Casa'da" -> "Casa", "'da")."""
    stem, apostrophe, ending = _APOSTROPHE_SPLIT.match(word).groups()  # type: ignore[union-attr]
    return stem, apostrophe + ending


def _span_stems(span: Sequence[str]) -> list[str] | None:
    """The heard words with the last one's case ending off; None when an ending sits
    mid-span (a case ending closes a name)."""
    if any(_APOSTROPHE.search(word) for word in span[:-1]):
        return None
    return [*span[:-1], _split_ending(span[-1])[0]]


def _reads_as(span: Sequence[str], wanted: Sequence[str]) -> bool:
    stems = _span_stems(span) if len(span) == len(wanted) else None
    return stems is not None and [_fold(w) for w in stems] == [_fold(w) for w in wanted]


def _sound_distance(span: Sequence[str], wanted: Sequence[str]) -> int:
    got, want = _sound("".join(span)), _sound("".join(wanted))
    return _edit_distance(got, want, max(len(got), len(want)))


def _near_cost(span: Sequence[str], target: str) -> int | None:
    """How far ``span`` (one token per heard word) is from ``target`` when it is a hearing
    of it spelled another way (lower is closer), else None."""
    wanted = _phrase_words(target)
    if not span or not wanted:
        return None
    if len(wanted) == 1:
        # A one-word phrase is one heard word, by the single-word rule (KRI-631).
        if len(span) != 1 or _match_word(span[0], target) is None:
            return None
        return _sound_distance(span, wanted)
    if len(span) == len(wanted):
        return _sound_distance(span, wanted) if _match_phrase(span, target) else None
    stems = _span_stems(span)
    if stems is None or abs(len(span) - len(wanted)) != 1:
        return None
    heard, written = [_fold(w) for w in stems], [_fold(w) for w in wanted]
    if "".join(heard) == "".join(written):
        return 0  # the same letters, spaced another way ("Kızkulesi" for "Kız Kulesi")
    anchors = {w for w in written if len(w) >= _ANCHOR_MIN_LETTERS}
    if anchors.isdisjoint(heard) or not _sounds_like("".join(stems), "".join(wanted)):
        return None
    return _sound_distance(stems, wanted)


def _phrase_hits(
    tokens: Sequence[str], joined_to_next: Sequence[bool], target: str
) -> list[NamedPhraseHit]:
    """Every run of ``tokens`` that says ``target``, in order, never overlapping.

    ``tokens[i]`` is the one word token of heard word ``i`` ("" when it has none or
    several); ``joined_to_next[i]`` is True when only spaces separate it from ``i + 1``.
    Exact runs are taken first; a near run never overlaps one. At one place the closest of
    the same count, one fewer and one more words wins (ties keep the same count).
    """
    size = len(_phrase_words(target))
    if not size:
        return []

    def usable(i: int, n: int) -> bool:
        return (
            n >= 1
            and i + n <= len(tokens)
            and all(tokens[i : i + n])
            and all(joined_to_next[i : i + n - 1])
        )

    wanted = _phrase_words(target)
    exact: list[NamedPhraseHit] = []
    i = 0
    while i + size <= len(tokens):
        if usable(i, size) and _reads_as(tokens[i : i + size], wanted):
            exact.append(NamedPhraseHit(i, i + size, True))
            i += size
        else:
            i += 1
    taken = {j for hit in exact for j in range(hit.start, hit.stop)}
    sizes = (size,) if size == 1 else (size, size - 1, size + 1)
    near: list[NamedPhraseHit] = []
    i = 0
    while i < len(tokens):
        best: tuple[int, int] | None = None
        for n in sizes:
            if not usable(i, n) or not taken.isdisjoint(range(i, i + n)):
                continue
            cost = _near_cost(tokens[i : i + n], target)
            if cost is not None and (best is None or cost < best[0]):
                best = (cost, n)
        if best is None:
            i += 1
            continue
        near.append(NamedPhraseHit(i, i + best[1], False))
        i += best[1]
    return sorted([*exact, *near], key=lambda hit: hit.start)


def _word_tokens(texts: Sequence[str]) -> tuple[list[list[re.Match[str]]], list[str], list[bool]]:
    """Per heard word: its token matches, its one token ("" unless exactly one), and whether
    only spaces join it to the next word ("leche," ends a phrase)."""
    singles = [list(_TOKEN.finditer(text)) for text in texts]
    tokens = [found[0].group(0) if len(found) == 1 else "" for found in singles]
    joined = [
        bool(
            tokens[i]
            and tokens[i + 1]
            and singles[i][0].end() == len(texts[i].rstrip())
            and singles[i + 1][0].start() == len(texts[i + 1]) - len(texts[i + 1].lstrip())
        )
        for i in range(len(texts) - 1)
    ]
    return singles, tokens, [*joined, False]


def find_named_phrase(words: Sequence[str], phrase: str) -> list[NamedPhraseHit]:
    """Where the heard ``words`` (one transcript word each, punctuation attached) say the
    creator-named ``phrase``: exactly, or heard another way under the narrow rule above.

    The one matcher behind the captions' respelling and the reaction-beat grounding, so a
    phrase the captions write the creator's way is a phrase the card hears (KRI-961).
    """
    texts = [str(word or "") for word in words]
    _singles, tokens, joined = _word_tokens(texts)
    return _phrase_hits(tokens, joined, phrase)


def _starts_sentence(before: str) -> bool:
    """True when the text before a word ends a sentence (or there is none)."""
    head = before.rstrip().rstrip(_OPENING_MARKS).rstrip()
    return not head or head.endswith(_SENTENCE_ENDS)


def _respelled(span: Sequence[str], target: str, *, sentence_start: bool) -> list[str] | None:
    """The words a near hit is written as. The same word count keeps KRI-793's casing; with
    another count the creator's own casing stands, capitalised only at a sentence start
    ("the Olloda bife" -> "the ojo de bife", "Olloda bife is" -> "Ojo de bife is")."""
    wanted = _phrase_words(target)
    if len(span) == len(wanted):
        return _match_phrase(span, target)
    _stem, ending = _split_ending(span[-1])
    out = [_recase(span[0][0], wanted[0]) if sentence_start else wanted[0], *wanted[1:]]
    out[-1] += ending
    return out


def _respell_phrases_in_text(text: str, phrases: Sequence[str]) -> str:
    for target in phrases:
        found = list(_TOKEN.finditer(text))
        joined = [
            bool(_SPACES_ONLY.fullmatch(text[a.end() : b.start()]))
            for a, b in zip(found, found[1:], strict=False)
        ]
        tokens = [m.group(0) for m in found]
        # From the end, so earlier offsets in ``found`` stay valid.
        for hit in reversed(_phrase_hits(tokens, [*joined, False], target)):
            if hit.exact:
                continue
            first, last = found[hit.start], found[hit.stop - 1]
            new = _respelled(
                tokens[hit.start : hit.stop],
                target,
                sentence_start=_starts_sentence(text[: first.start()]),
            )
            if new is None:
                continue
            if len(new) == hit.stop - hit.start:
                # Word for word, the spaces as heard.
                for match, word in reversed(list(zip(found[hit.start : hit.stop], new))):
                    text = text[: match.start()] + word + text[match.end() :]
            else:
                text = text[: first.start()] + " ".join(new) + text[last.end() :]
    return text


def respell_text(text: str, targets: Sequence[str]) -> str:
    """``text`` with caption words that sound like a target replaced by that target."""
    if not text or not targets:
        return text
    phrases = [t for t in targets if len(_phrase_words(t)) > 1]
    words = [t for t in targets if len(_phrase_words(t)) == 1]
    text = _respell_phrases_in_text(text, phrases)
    if not words:
        return text

    def _swap(found: re.Match[str]) -> str:
        word = found.group(0)
        for target in words:
            replaced = _match_word(word, target)
            if replaced is not None:
                return replaced
        return word

    return _TOKEN.sub(_swap, text)


def _time(word: dict[str, Any], key: str) -> float | None:
    value = word.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _split_word(word: dict[str, Any], first: str, second: str) -> list[dict[str, Any]]:
    """One heard word written as two: its time shared by their letters."""
    start, end = _time(word, "start_s"), _time(word, "end_s")
    if start is None or end is None or end < start:
        return [{**word, "text": first}, {**word, "text": second}]
    a, b = len(_fold(first)), len(_fold(second))
    cut = round(start + (end - start) * a / (a + b), 3) if a + b else start
    cut = min(max(cut, start), end)
    return [{**word, "text": first, "end_s": cut}, {**word, "text": second, "start_s": cut}]


def _merge_words(first: dict[str, Any], second: dict[str, Any], text: str) -> dict[str, Any]:
    """Two heard words written as one: from the first's start to the second's end."""
    merged = {**first, "text": text}
    if "end_s" in second:
        merged["end_s"] = second["end_s"]
    a, b = first.get("confidence"), second.get("confidence")
    if all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in (a, b)):
        merged["confidence"] = min(a, b)  # type: ignore[type-var]
    return merged


def _groups(heard: Sequence[str], new: Sequence[str]) -> list[tuple[list[int], list[int]]]:
    """Which heard words become which written words (a count differs by at most one).

    Word for word, except the one heard word split in two (or the two heard words merged)
    where the letters fit best: "Olloda bife" -> "ojo de" + "bife", not "ojo" + "de bife".
    """
    n, k = len(heard), len(new)
    if n == k:
        return [([j], [j]) for j in range(n)]
    options: list[list[tuple[list[int], list[int]]]] = []
    if n == k - 1:
        for s in range(n):
            options.append(
                [([j], [j]) for j in range(s)]
                + [([s], [s, s + 1])]
                + [([j], [j + 1]) for j in range(s + 1, n)]
            )
    else:
        for m in range(k):
            options.append(
                [([j], [j]) for j in range(m)]
                + [([m, m + 1], [m])]
                + [([j + 1], [j]) for j in range(m + 1, k)]
            )

    def cost(groups: list[tuple[list[int], list[int]]]) -> int:
        total = 0
        for hs, ws in groups:
            got = _fold("".join(heard[h] for h in hs))
            want = _fold("".join(new[w] for w in ws))
            total += _edit_distance(got, want, max(len(got), len(want)))
        return total

    return min(options, key=cost)  # the first of equals: the earliest split or merge


def _respelled_words(
    words: Sequence[dict[str, Any]],
    singles: Sequence[list[re.Match[str]]],
    tokens: Sequence[str],
    new: Sequence[str],
) -> list[dict[str, Any]]:
    """The heard ``words`` of one hit written as ``new``; text around a word's token
    (punctuation) stays on the written word at that edge."""
    texts = [str(w.get("text") or "") for w in words]
    head = [text[: found[0].start()] for text, found in zip(texts, singles, strict=True)]
    tail = [text[found[0].end() :] for text, found in zip(texts, singles, strict=True)]
    out: list[dict[str, Any]] = []
    for hs, ws in _groups(tokens, new):
        if len(hs) == 1 and len(ws) == 1:
            out.append({**words[hs[0]], "text": head[hs[0]] + new[ws[0]] + tail[hs[0]]})
        elif len(hs) == 1:
            h = hs[0]
            out.extend(_split_word(words[h], head[h] + new[ws[0]], new[ws[1]] + tail[h]))
        else:
            out.append(
                _merge_words(words[hs[0]], words[hs[1]], head[hs[0]] + new[ws[0]] + tail[hs[1]])
            )
    return out


def _respell_word_list(words: list[dict[str, Any]], targets: Sequence[str]) -> list[dict[str, Any]]:
    """A cue's timed words with the targets applied; a phrase spans adjacent words only.

    A word joins the next one into a phrase only when each holds exactly one token and no
    punctuation sits between them ("leche," ends a phrase). The same word count keeps every
    timing; one more or fewer (KRI-961) re-times only the words of that hit.
    """
    words = list(words)
    for target in (t for t in targets if len(_phrase_words(t)) > 1):
        texts = [str(w.get("text") or "") for w in words]
        singles, tokens, joined = _word_tokens(texts)
        out: list[dict[str, Any]] = []
        cursor = 0
        for hit in _phrase_hits(tokens, joined, target):
            if hit.exact:
                continue
            # The cue text before the hit, as the cue text pass sees it.
            prefix = texts[hit.start][: singles[hit.start][0].start()]
            before = " ".join([*texts[: hit.start], prefix])
            new = _respelled(
                tokens[hit.start : hit.stop], target, sentence_start=_starts_sentence(before)
            )
            if new is None:
                continue
            out.extend(words[cursor : hit.start])
            out.extend(
                _respelled_words(
                    words[hit.start : hit.stop],
                    singles[hit.start : hit.stop],
                    tokens[hit.start : hit.stop],
                    new,
                )
            )
            cursor = hit.stop
        # The next target reads the respelled words, exactly as the cue text pass does
        # ("dulse de leche condensata" with "dulce de leche" and "leche condensada").
        words = [*out, *words[cursor:]]
    single_targets = [t for t in targets if len(_phrase_words(t)) == 1]
    return [
        {**word, "text": respell_text(str(word.get("text") or ""), single_targets)}
        for word in words
    ]


def is_mishearing(shown: str, target: str) -> bool:
    """True when ``shown`` (caption words) is a hearing of ``target`` spelled another way.

    The same narrow rule the respelling applies, so a check after render calls exactly
    what the render would have fixed a miss (KRI-793): "Meropa" for "Merope", "Ole de la
    Casa" for "Oblea de la Casa", "Olloda bife" for "ojo de bife" (KRI-961). The target
    itself, in any case, is never a mishearing.
    """
    words = [m.group(0) for m in _TOKEN.finditer(shown)]
    return _near_cost(words, target) is not None


def apply_spelling_to_cues(
    cues: Iterable[dict[str, Any]], targets: Sequence[str]
) -> list[dict[str, Any]]:
    """Caption cues (text and attached word timings) with the creator's spellings applied.

    The cue's own start and end never change; a word's timing changes only when a phrase
    was heard as one word more or fewer (KRI-961), and only inside that hit. A touched cue
    is a copy.
    """
    cues = list(cues)
    if not targets:
        return cues
    out: list[dict[str, Any]] = []
    for cue in cues:
        text = respell_text(str(cue.get("text") or ""), targets)
        words = cue.get("words")
        new_words = (
            _respell_word_list(words, targets)
            if isinstance(words, list) and all(isinstance(w, dict) for w in words)
            else words
        )
        if text == cue.get("text") and new_words == words:
            out.append(cue)
            continue
        changed = {**cue, "text": text}
        if new_words is not None:
            changed["words"] = new_words
        out.append(changed)
    return out
