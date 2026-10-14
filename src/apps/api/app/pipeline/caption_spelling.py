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
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

MAX_ASKS = 12
_MIN_TARGET_LEN = 5

_FOLD = str.maketrans(
    {"ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u", "â": "a", "î": "i", "û": "u"}
)


def _fold(text: str) -> str:
    """Lowercase and strip Turkish diacritics; "İ"/"I" both become "i"."""
    return (text or "").replace("İ", "i").replace("I", "i").lower().translate(_FOLD)


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
    """``word`` respelled as ``target`` (with its ending kept), or None to leave it."""
    stem_part, apostrophe, tail = re.match(r"([^'’]*)(['’]?)(.*)", word).groups()  # type: ignore[union-attr]
    if apostrophe:
        # "komünite'nin": the creator-visible ending is already split off.
        if not _sounds_like(stem_part, target):
            return None
        return _recase(word[0], target) + apostrophe + tail
    low = _fold(word)
    if low == _fold(target):
        return None
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


def respell_text(text: str, targets: Sequence[str]) -> str:
    """``text`` with caption words that sound like a target replaced by that target."""
    if not text or not targets:
        return text

    def _swap(found: re.Match[str]) -> str:
        word = found.group(0)
        for target in targets:
            replaced = _match_word(word, target)
            if replaced is not None:
                return replaced
        return word

    return _TOKEN.sub(_swap, text)


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
            [{**w, "text": respell_text(str(w.get("text") or ""), targets)} for w in words]
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
