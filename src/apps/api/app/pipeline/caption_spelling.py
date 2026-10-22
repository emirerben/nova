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
approved brief). Such a phrase is matched only as the same number of adjacent words with
nothing but spaces between them, so every word keeps its own timing; the same sound rule
then runs on the words joined ("Ole de la Casa" -> "Oblea de la Casa").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
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


def _respell_phrase_tokens(
    tokens: Sequence[str], joined_to_next: Sequence[bool], target: str
) -> dict[int, str]:
    """Replacements (token index -> new text) for every run of ``tokens`` heard as ``target``.

    ``joined_to_next[i]`` is True when only spaces separate token ``i`` from ``i + 1``.
    """
    size = len(_phrase_words(target))
    out: dict[int, str] = {}
    i = 0
    while i + size <= len(tokens):
        if all(tokens[i : i + size]) and all(joined_to_next[i : i + size - 1]):
            replaced = _match_phrase(tokens[i : i + size], target)
            if replaced is not None:
                out.update({i + k: word for k, word in enumerate(replaced)})
                i += size
                continue
        i += 1
    return out


def _respell_phrases_in_text(text: str, phrases: Sequence[str]) -> str:
    for target in phrases:
        found = list(_TOKEN.finditer(text))
        joined = [
            bool(_SPACES_ONLY.fullmatch(text[a.end() : b.start()]))
            for a, b in zip(found, found[1:], strict=False)
        ]
        swaps = _respell_phrase_tokens([m.group(0) for m in found], [*joined, False], target)
        for index in sorted(swaps, reverse=True):
            match = found[index]
            text = text[: match.start()] + swaps[index] + text[match.end() :]
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


def _respell_word_list(words: list[dict[str, Any]], targets: Sequence[str]) -> list[dict[str, Any]]:
    """A cue's timed words with the targets applied; a phrase spans adjacent words only.

    A word joins the next one into a phrase only when each holds exactly one token and no
    punctuation sits between them ("leche," ends a phrase).
    """
    texts = [str(w.get("text") or "") for w in words]
    singles = [list(_TOKEN.finditer(text)) for text in texts]
    tokens = [found[0].group(0) if len(found) == 1 else "" for found in singles]
    joined = [
        bool(
            tokens[i]
            and tokens[i + 1]
            and singles[i][0].end() == len(texts[i].rstrip())
            and singles[i + 1][0].start() == len(texts[i + 1]) - len(texts[i + 1].lstrip())
        )
        for i in range(len(words) - 1)
    ]
    new_texts = list(texts)
    for target in (t for t in targets if len(_phrase_words(t)) > 1):
        for index, word in _respell_phrase_tokens(tokens, [*joined, False], target).items():
            match, text = singles[index][0], new_texts[index]
            new_texts[index] = text[: match.start()] + word + text[match.end() :]
            # The next target reads the respelled word, exactly as the cue text pass does
            # ("dulse de leche condensata" with "dulce de leche" and "leche condensada").
            tokens[index] = word
            singles[index] = list(_TOKEN.finditer(new_texts[index]))
    single_targets = [t for t in targets if len(_phrase_words(t)) == 1]
    return [
        {**word, "text": respell_text(new_text, single_targets)}
        for word, new_text in zip(words, new_texts, strict=True)
    ]


def is_mishearing(shown: str, target: str) -> bool:
    """True when ``shown`` (caption words) is a hearing of ``target`` spelled another way.

    The same narrow rule the respelling applies, so a check after render calls exactly
    what the render would have fixed a miss (KRI-793): "Meropa" for "Merope", "Ole de la
    Casa" for "Oblea de la Casa". The target itself, in any case, is never a mishearing.
    """
    words = [m.group(0) for m in _TOKEN.finditer(shown)]
    wanted = _phrase_words(target)
    if not words or not wanted:
        return False
    if len(wanted) == 1:
        return len(words) == 1 and _match_word(words[0], target) is not None
    return _match_phrase(words, target) is not None


def apply_spelling_to_cues(
    cues: Iterable[dict[str, Any]], targets: Sequence[str]
) -> list[dict[str, Any]]:
    """Caption cues (text and attached word timings) with the creator's spellings applied.

    Timings are untouched; only ``text`` strings change, in a copy of each touched cue.
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
