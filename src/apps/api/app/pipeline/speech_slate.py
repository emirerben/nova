"""Off-script pre-roll at the very start of a take ("okay, starting", "we're recording", "three,
two, one", "is this on?", "tamam, kayıttayız", "başlıyorum").

Speech cleanup cuts pauses, filler sounds and abandoned takes; a creator who says "okay,
starting" before the first real sentence restarts nothing, so none of those catch it (Çiğdem S2
"ss2b" 2026-11-08, KRI-1326; yorum3 2026-11-09, KRI-1348). This module finds that opening,
deterministically, and hands it to the cut planner as a retake-style word span, so it gets the
same outward snapping, voice-guard exemption and receipts as an abandoned take.

The rule is about word ROLES, not phrases (failure modes:
``docs/reviews/cigdem-s2-failure-modes.md`` A1-A14 and
``docs/reviews/kri-1348-preroll-failure-modes.md`` F1-F19, P1-P7). Each word of the opening gets
one role: filler ("okay", "tamam"), glue ("we", "is", "this", "mı"), production
("recording", "starting", "mic", "kayıt", "başlıyorum"), countdown number, or cue ("go",
"let's", "is this on", "hadi", "hazır mıyız"). An utterance is pre-roll when EVERY word has a
role and it has an anchor: a production word, a countdown (two or more numbers), or a cue
followed by a clear fresh-start pause before the real talk. New phrasings are new combinations and
need no change; a word with no role makes the utterance real speech, so a real first sentence
("Okay, so here's the thing") is kept.

Precision over recall:

* only utterances that start at word 0 are read, one after another, and the scan stops at the
  first real one (a slate mid-take is never cut); a line of glue words with no anchor ("This is
  it.") also stops it, so a later aside never sweeps a real opening into the cut;
* an utterance ends at sentence-final punctuation, a gap of ``_MIN_GAP_S`` or a detected silence
  starting inside its last word; at most ``_MAX_UTTERANCE_WORDS`` words and ``_MAX_UTTERANCE_S``
  long, so a transcript that glues the opening to the content never matches;
* cue-only stretches ("Let's go!", "Is this on? Okay.") also need a pause of at least
  ``_MIN_CUE_PAUSE_S`` right before the real talk: a hook that flows into the content is kept;
  words addressed to the viewer ("you", "siz", "mısınız") have no role, so "Are you ready?" or
  "Hazır mısınız?" asked of the audience is never pre-roll, and words that open real hooks as
  often as pre-roll ("Wait.", "Ready?", "Sorry.") are never an anchor;
* an article before a production word makes it a noun in a real sentence ("It's a start.");
* numbers add up across the opening: two or more ("Three, … two, … one.") are a countdown, a
  lone number ("Five.") is a hook;
* a topic-able production noun (mic, camera, sound, light, test …) counts only when the rest of
  the take never mentions the same thing (a mic review opening "Mic check." is kept);
* the whole pre-roll ends by ``_MAX_PREROLL_END_S`` and at least ``_MIN_WORDS_AFTER`` words stay.

Pure: no I/O, no settings. Callers gate it on ``SPEECH_CLEANUP_SLATE_CUT_ENABLED`` and on the
same retake switches every other retake cut needs (it is cut and counted as a retake).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.text_fold import loose_text

_MAX_UTTERANCE_WORDS = 6
_MAX_UTTERANCE_S = 3.0
_MAX_PREROLL_WORDS = 14
_MAX_PREROLL_END_S = 8.0
_MIN_GAP_S = 0.2
_MIN_SILENCE_S = 0.15
_MIN_CUE_PAUSE_S = 0.6
_MIN_COUNTDOWN_NUMBERS = 2
_MIN_WORDS_AFTER = 3

# Word lists are written naturally (case, Turkish letters, apostrophes) and folded once below the
# way a transcript word is. An entry ending in "*" is a stem: it matches any word starting with
# it. Stems are long and specific on purpose ("başlıyor*", never "baş*"); a stem that collides
# with an unrelated word only matters if every other word of the utterance also has a role.

# Allowed anywhere in pre-roll; never enough alone.
_FILLER = (
    "okay ok okey okie alright all right so um uh erm er hmm mm oh ah well yeah yes yep no "
    "tamam tamamdır evet peki şey hı hıı ıı ıh ee eee aa hm yani işte of tamamdir"
)
# Words addressed to the AUDIENCE ("you", "siz", "mısınız") are deliberately absent: "Are you
# ready?", "Hazır mısınız?" asked of the viewer is a hook, not pre-roll. A line made of glue words
# and no anchor ("This is it.") may be the real opening, so it ends the scan (F19).
_GLUE = (
    "i im ive we were weve it its this that thats is are am was be been the a an "
    "now again here there then just me us to do does can gonna going let get got in "
    "ben biz bu şu şimdi artık mı mi mu mü muyum miyim "
    "da de ve var oldu olduk oluyor geliyor"
)
# Interjections that open real hooks as often as pre-roll ("Wait.", "Ready?", "Dur!"): allowed,
# never an anchor. Alone they are not a sentence, so they do not end the scan ("Wait, … the
# light, … okay." is split by whisper at every pause).
_SOFT = "wait hold ready hazır dur bekle"
# An article before a production word makes it a noun in a real sentence ("That's a record.",
# "It's a start.", "Bu bir kayıt.").
_ARTICLES = frozenset({"a", "an", "the", "bir"})
# Talk about the recording itself. Enough with a normal utterance boundary.
_PRODUCTION = (
    "recording record recorded records rolling roll rolls starting start started starts "
    "begin beginning begins check checking "
    "kayıt* kaydet* kaydedi* kaydedil* başlıyor* başlıyom başlayalım başlayabiliriz "
    "başladı* başla başlasın çekiyor* çekiyoz çekim* kontrol*"
)
# Production nouns a video could be ABOUT: they count only when the rest of the take never
# mentions the same concept (key = concept).
_TOPIC = {
    "mic": "mic mics microphone microphones mikrofon*",
    "camera": "camera cameras kamera*",
    "sound": "sound audio ses sesi sesim sesler",
    "light": "light lights lighting ışık*",
    "focus": "focus focused odak*",
    "test": "test tests testing tested testi deneme*",
}
# Pre-roll only with a clear fresh-start pause right before the real talk. Never an apology
# ("Sorry." can open an apology video).
_CUE = (
    "set go lets action second sec moment on hear see working good take "
    "hazırım hazırız mıyız miyiz muyuz müyüz hadi haydi saniye gidiyoruz "
    "duyuluyor* duyuyor* görünüyor* aksiyon"
)
_NUMBERS = (
    "one two three four five six seven eight nine ten 1 2 3 4 5 6 7 8 9 10 321 "
    "bir iki üç dört beş altı yedi sekiz dokuz"
)

_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)]*$")
_CLAUSE_GOES_ON = re.compile(r"[,;:]$")
# Words a sentence cannot end on: a fragment ending on one is mid-clause ("Wait, the" / "light").
_OPEN_ENDS = frozenset({"the", "a", "an", "to", "of", "in", "and", "bu", "şu", "ve", "bir"})
_STRIP = re.compile(r"[^\w\s]")


def _fold(text: str) -> str:
    """The shared loose fold (Turkish-aware case, diacritics stripped: "KAYITTAYIZ" ==
    "kayıttayız" == "kayittayiz"), apostrophes and punctuation removed."""
    text = loose_text(text).replace("'", "").replace("’", "")
    return " ".join(_STRIP.sub(" ", text).split())


class _Lexicon:
    def __init__(self, entries: str) -> None:
        exact: set[str] = set()
        stems: list[str] = []
        for entry in entries.split():
            if entry.endswith("*"):
                stems.append(_fold(entry[:-1]))
            else:
                exact.add(_fold(entry))
        self.exact = frozenset(exact)
        self.stems = tuple(stems)

    def __contains__(self, token: str) -> bool:
        return token in self.exact or any(token.startswith(stem) for stem in self.stems)


_FILLER_LEX = _Lexicon(_FILLER)
_GLUE_LEX = _Lexicon(_GLUE)
_PRODUCTION_LEX = _Lexicon(_PRODUCTION)
_TOPIC_LEX = {concept: _Lexicon(entries) for concept, entries in _TOPIC.items()}
_CUE_LEX = _Lexicon(_CUE)
_SOFT_LEX = _Lexicon(_SOFT)
_OPEN_END_KEYS = frozenset(_fold(word) for word in _OPEN_ENDS)
_NUMBER_LEX = _Lexicon(_NUMBERS)


def _value(raw: Any, *names: str) -> Any:
    for name in names:
        if isinstance(raw, Mapping):
            if name in raw:
                return raw[name]
        elif hasattr(raw, name):
            return getattr(raw, name)
    return None


def _words(words: Sequence[Any]) -> list[tuple[str, float, float]] | None:
    out: list[tuple[str, float, float]] = []
    for raw in words:
        text = _value(raw, "text", "word")
        start = _value(raw, "start_s", "start")
        end = _value(raw, "end_s", "end")
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            lo, hi = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if hi < lo:
            return None
        out.append((text.strip(), lo, hi))
    return out


def _boundary_silences(
    words: list[tuple[str, float, float]],
    j: int,
    silences: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    # Whisper stamps can stretch over a pause (onto this word's end or the next word's start), so
    # a silence that starts after this word began and before the next word's speech marks the
    # boundary too. One that starts later belongs to a later word (ss2b: the pause after
    # "recording" must not end the utterance at "we're").
    _text, start, _end = words[j]
    next_start = words[j + 1][1]
    return [(lo, hi) for lo, hi in silences if start + 0.05 <= lo <= next_start + 0.15]


def _ends_utterance(
    words: list[tuple[str, float, float]],
    j: int,
    silences: Sequence[tuple[float, float]],
) -> bool:
    text, _start, end = words[j]
    if _SENTENCE_END.search(text):
        return True
    if words[j + 1][1] - end >= _MIN_GAP_S:
        return True
    return any(hi - lo >= _MIN_SILENCE_S for lo, hi in _boundary_silences(words, j, silences))


def _mid_clause(text: str) -> bool:
    folded = _fold(text).split()
    return bool(_CLAUSE_GOES_ON.search(text)) or bool(folded and folded[-1] in _OPEN_END_KEYS)


def _topic_concept(token: str) -> str | None:
    for concept, lexicon in _TOPIC_LEX.items():
        if token in lexicon:
            return concept
    return None


class _Roles:
    """What one utterance contributes; ``real`` = a word with no production role."""

    __slots__ = ("concepts", "cue", "filler_only", "glue", "numbers", "production", "real", "soft")

    def __init__(self) -> None:
        self.concepts: set[str] = set()
        self.real = False
        self.filler_only = True
        self.glue = False
        self.soft = False
        self.production = False
        self.numbers = 0
        self.cue = False


def _roles(tokens: list[str]) -> _Roles:
    roles = _Roles()
    folded = [part for token in tokens for part in _fold(token).split() if part]
    previous = ""
    for token in folded:
        prior, previous = previous, token
        if token in _FILLER_LEX:
            continue
        roles.filler_only = False
        if token in _PRODUCTION_LEX:
            if prior in _ARTICLES:
                roles.real = True  # "a record", "the start": a noun in a real sentence (F2)
                return roles
            roles.production = True
            continue
        concept = _topic_concept(token)
        if concept is not None:
            roles.concepts.add(concept)
            roles.production = True
            continue
        if token in _NUMBER_LEX:
            roles.numbers += 1
        elif token in _CUE_LEX:
            roles.cue = True
        elif token in _SOFT_LEX:
            roles.soft = True
        elif token in _GLUE_LEX:
            roles.glue = True
        else:
            roles.real = True  # a word with no production role (F1, F2, F9)
            return roles
    return roles


def _fresh_start_pause(
    words: list[tuple[str, float, float]],
    first: int,
    last: int,
    silences: Sequence[tuple[float, float]],
) -> float:
    """The pause right before word ``last + 1`` (the real talk): the stamped gap, or a detected
    silence that starts inside words ``first..last`` and runs up to that word (whisper often
    stretches a stamp over the pause, so the silence is the reliable measure)."""
    gap = words[last + 1][1] - words[last][2]
    lo_bound = words[first][1] + 0.05
    next_start = words[last + 1][1]
    longest = max(
        (
            hi - lo
            for lo, hi in silences
            if lo_bound <= lo <= next_start + 0.15 and hi >= next_start - 0.15
        ),
        default=0.0,
    )
    return max(gap, longest)


def _rest_concepts(words: list[tuple[str, float, float]], start: int) -> frozenset[str]:
    concepts: set[str] = set()
    for text, _s, _e in words[start:]:
        for token in _fold(text).split():
            concept = _topic_concept(token)
            if concept is not None:
                concepts.add(concept)
    return frozenset(concepts)


def opening_slate_span(
    words: Sequence[Any],
    *,
    silences: Sequence[tuple[float, float]] = (),
) -> tuple[int, int] | None:
    """The inclusive word-index span of off-script pre-roll opening the take, else None.

    ``words`` are the take's timed words (``text``/``word``, ``start_s``/``start``,
    ``end_s``/``end``, as dicts or objects); ``silences`` detected silence spans in seconds.
    """
    parsed = _words(words)
    if not parsed or len(parsed) < 1 + _MIN_WORDS_AFTER:
        return None
    clean_silences: list[tuple[float, float]] = []
    for span in silences or ():
        try:
            lo, hi = float(span[0]), float(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if hi > lo:
            clean_silences.append((lo, hi))
    # The opening is read utterance by utterance as one running stretch: numbers add up across
    # pauses ("Three, … two, … one."), and a cue ("Is this on?", "Let's go.") is settled by the
    # pause before the real talk, not by the pause right after it ("Is this on? Okay. [pause]").
    cut_end: int | None = None  # last word of confirmed pre-roll
    cue_end: int | None = None  # last word of a cue stretch waiting for its fresh-start pause
    cue_first = 0
    numbers = 0
    concepts: list[tuple[int, set[str]]] = []  # (utterance end, topic nouns it used)
    first = 0  # first word of the utterance being read
    for j in range(min(_MAX_PREROLL_WORDS, len(parsed) - 1)):
        if parsed[j][2] > _MAX_PREROLL_END_S:
            break
        if j - first + 1 > _MAX_UTTERANCE_WORDS:
            break  # no boundary: the opening runs into the content (F4)
        if not _ends_utterance(parsed, j, clean_silences):
            continue
        if parsed[j][2] - parsed[first][1] > _MAX_UTTERANCE_S:
            break  # a slow preamble is not a slate (A7)
        roles = _roles([text for text, _s, _e in parsed[first : j + 1]])
        if roles.real:
            break
        numbers += roles.numbers
        if roles.concepts:
            concepts.append((j, roles.concepts))
        if roles.production or (roles.numbers and numbers >= _MIN_COUNTDOWN_NUMBERS):
            cut_end, cue_end = j, None
        elif roles.cue:
            if cue_end is None:
                cue_first = first
            cue_end = j
        elif not roles.glue:
            # Fillers ("Okay."), an interjection ("Wait,") or a countdown still under way
            # ("Three,"): keep reading. After pre-roll the fillers go too ("We're recording.
            # Okay.").
            if cue_end is not None:
                cue_end = j
            elif cut_end is not None and roles.filler_only:
                cut_end = j
        elif _mid_clause(parsed[j][0]) and (
            _fresh_start_pause(parsed, first, j, clean_silences) < _MIN_CUE_PAUSE_S
        ):
            # A fragment whisper split off at a short pause mid-clause ("Wait, the" / "light,"):
            # it joins the next piece and is judged with it.
            continue
        else:
            # A line of glue words with no anchor ("This is it.", "Here we are", unpunctuated
            # too) may be the real opening: the scan stops, so a later aside never sweeps it
            # in (F19).
            break
        first = j + 1
    if cue_end is not None and (
        _fresh_start_pause(parsed, cue_first, cue_end, clean_silences) >= _MIN_CUE_PAUSE_S
    ):
        cut_end = cue_end
    if cut_end is None or len(parsed) - (cut_end + 1) < _MIN_WORDS_AFTER:
        return None
    used = {concept for end, found in concepts if end <= cut_end for concept in found}
    if used & _rest_concepts(parsed, cut_end + 1):
        return None  # the video talks about the mic / camera / light it checked (F5)
    return (0, cut_end)
