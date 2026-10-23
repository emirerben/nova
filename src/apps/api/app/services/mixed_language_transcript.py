"""Which stretches of a mixed-language take did each transcription pass get right? (KRI-843)

whisper-1 picks ONE language for a whole take from its own audio, then writes a TRANSLATION
into it for every stretch said in another. Don Julio T1 (25.7 s): Yasin asks "What's your name?"
in English, Lucas the waiter explains the sauces in Spanish, a man says "Cheers!", and the table
chats in Turkish. Auto-detect says Turkish (the chatter is the longest block), so the question
comes back as "Evet, adın ne?" and the quoted start line is never found; forcing English keeps
the question and throws Lucas away; forcing Spanish translates the question. No single language
is right for the whole take. KRI-808's ``clip_speech_check`` decides per 2-4 s clip; this decides
per stretch inside one take.

So the language is decided per stretch, from evidence, and never forced onto the whole take:

* Gemini's transcript of the same audio is an independent listener. Its sentences name the
  languages the take holds (``reference_languages``); only a reference with two or more makes a
  take mixed, and each extra language gets one more transcription pass (the caller's job).
* Every pass is aligned to the reference IN ORDER (``_Reference.heard``): a word is heard only
  where the reference has it. The Turkish translation's "evet"/"ne" at 2 s must not match the
  chatter's "evet"/"ne" at 24 s, or the translation would beat the faithful English.
* Each pass's sentences compete for the stretch they cover (``merge_passes``): the non-overlapping
  set that the reference confirms best wins, ties keep the first pass. A pass that dropped speech
  and printed one long word over it ("Gracias." for 7 s) confirms one word and loses to the
  many-word sentence it overlaps.
* The first pass stands wherever no other pass is confirmed better. A stretch nothing confirms is
  NOT dropped (KRI-808 drops a whole clip's words, but there nothing else is cut): wordless sound
  inside a take reads as a pause to the speech-cleanup cut plan, which then removes it. On T1 that
  cut 12-14.4 s of table sound; a short Gemini reference erased 13-20 s of real Turkish speech.
  Only the first-pass stretches an extra pass replaced are gone. Past what the reference covers
  (Gemini only hears the first 30 s) no extra pass is a candidate at all.

Pure functions; the caller supplies the transcription passes.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.services.clip_speech_check import MIN_HEARD_SHARE, clean_words
from app.services.speech_segments import sentence_groups
from app.services.transcript_quality import speech_tokens
from app.text_fold import loose_case

# More extra passes than this is never worth the paid calls: the languages the reference
# shows most come first.
MAX_EXTRA_PASSES = 3

# -- 1. which languages does the reference speak? ----------------------------------------------
#
# A sentence names a language when at least two of its words are that language's function words
# (or letters only it uses) and the next language has less than half as many; a sentence with ONE
# such word names it too when no other language has a word in it. A language is in the take when
# the sentences naming it add up to two hits. The lists are accent-folded ("acá" = "aca") and
# disjoint on purpose -- a word in two lists would vote for both -- so a word several languages
# share (de, la, para, un, en, ...) or one an English sentence uses (as, do, um, tag, bon) is in
# none. Conservative by design: a sentence that does not clearly name one language names none.
_LEXICON_WORDS: dict[str, str] = {
    "en": """the and you your you're what what's who where when why how that that's this these
        those with have has had are was were will would could should they them their there here
        we we're i'm i'll i've it's don't can't won't isn't didn't gonna wanna yeah hey hello hi
        thanks thank please just really very much some any from about because going know want
        let's okay right well good not but if my he she his her its our us does did is to of it
        so be at by all today guys everyone welcome watching""",
    "tr": """ve bir bu ne evet hayır hayir değil degil çok cok ama için icin gibi daha var yok
        nasıl nasil neden ben sen biz siz onlar yani tamam hadi bak bakın bile hep sadece
        gerçekten gercekten güzel guzel canım canim abi merhaba teşekkürler tesekkurler lütfen
        lutfen tabii belki şimdi simdi bugün bugun çünkü cunku ki mı mu mü hiçbir hicbir şey sey
        kim biraz bence herkes arkadaşlar selam nerede""",
    "es": """el los las una unos unas y con del es son estoy estás estamos están este esta estas
        estos está esto eso ese esa cómo cuándo dónde quién cuál pero muy más también bueno
        buena buenos buenas gracias hola tenemos tiene tienen tienes tengo hay aquí acá ahí allí
        ahora siempre nunca hoy mañana yo tú usted ustedes sí mucho mucha muchos quiero puedo
        puede podemos somos soy eres su sus vamos bien nosotros ellos ella""",
    "de": """der die das und ist nicht ich du wir ihr ein eine einen mit von zu den dem auf für auch
        aber wie wo hier ja nein danke bitte hallo sehr schon noch mal doch oder wenn weil dass
        kein keine haben sind nur jetzt heute wirklich guten""",
    "fr": """les des une et est qui pas je elle nous vous ils ce cette avec pour dans sur très
        merci bonjour oui c'est j'ai n'est qu'est ici sommes suis beaucoup bonne journée aussi
        comme être""",
    "it": """gli che sono siamo questo questa quello anche molto bene grazie ciao buongiorno
        buonasera perché cosa più già c'è hanno prego nel nella della delle degli sul sulla alla
        allo noi voi loro oggi dove sempre""",
    "pt": """não são você vocês obrigado obrigada olá muito também isso isto então ainda nós ele
        ela eles pelo pela estou tenho tem mais bom boa""",
}
# Letters that name a language by themselves (checked on the word as written, not folded).
_MARK_CHARS: dict[str, str] = {"tr": "ğşı", "es": "ñ", "de": "ßä", "fr": "ûœëï", "pt": "ãõ"}


def _fold_word(word: str) -> str:
    return loose_case(word.replace("’", "'"))


LEXICONS: dict[str, frozenset[str]] = {
    language: frozenset(_fold_word(word) for word in words.split())
    for language, words in _LEXICON_WORDS.items()
}
_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")
# A sentence with no punctuation (Gemini sometimes writes a paragraph) is read in windows.
_MAX_SENTENCE_TOKENS = 16
_WINDOW_TOKENS = 8
_MIN_SENTENCE_HITS = 2
_LEAD_FACTOR = 2
# A language is in the take when the sentences naming it add up to this many hits.
_MIN_LANGUAGE_EVIDENCE = 2


def _sentence_hits(sentence: str) -> dict[str, int]:
    hits: dict[str, int] = defaultdict(int)
    text = unicodedata.normalize("NFC", sentence.replace("’", "'"))
    for raw in _WORD_RE.findall(text):
        written = raw.lower().replace("i\u0307", "i")
        folded = _fold_word(raw)
        for language, words in LEXICONS.items():
            marks = _MARK_CHARS.get(language, "")
            if (
                folded in words
                or any(ch in written for ch in marks)
                or (language == "tr" and "İ" in raw)
            ):
                hits[language] += 1
    if "¿" in sentence or "¡" in sentence:
        hits["es"] += 2
    return hits


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for sentence in _SENTENCE_END.split(text or ""):
        words = sentence.split()
        if len(words) <= _MAX_SENTENCE_TOKENS:
            out.append(sentence)
            continue
        out.extend(
            " ".join(words[i : i + _WINDOW_TOKENS]) for i in range(0, len(words), _WINDOW_TOKENS)
        )
    return out


def reference_languages(text: str | None) -> list[str]:
    """The languages a reference transcript speaks, the most evidenced first.

    Sentence by sentence: a language counts only for a sentence that clearly names it (see the
    lists above). A reference that names two or more makes the take a mixed one.
    """
    evidence: dict[str, int] = defaultdict(int)
    for sentence in _sentences(str(text or "")):
        hits = sorted(_sentence_hits(sentence).items(), key=lambda item: -item[1])
        if not hits:
            continue
        best_language, best = hits[0]
        second = hits[1][1] if len(hits) > 1 else 0
        clear = best >= _MIN_SENTENCE_HITS and best >= _LEAD_FACTOR * second
        if clear or (best == 1 and second == 0):
            evidence[best_language] += best
    ranked = sorted(evidence.items(), key=lambda item: (-item[1], item[0]))
    return [language for language, count in ranked if count >= _MIN_LANGUAGE_EVIDENCE]


# -- 2. which words did the reference hear, in order? --------------------------------------------

# Only a word this long may match a spelling variant ("tradicional"/"tradicionales",
# "chivichurri"/"chimichurri", "Lukas"/"Lucas"), starting with the same two letters and within
# one edit per five letters; short words ("y", "en", "a", "de") match exactly or not at all.
_FUZZY_MIN_LEN = 5
_FUZZY_PREFIX = 2
_FUZZY_EDITS_PER_LETTERS = 5
# A word the reference repeats (a decoder loop, "video" x 40) is matched at the occurrences
# nearest its place only: keeps the alignment linear on a loop.
_MAX_OCCURRENCES = 24
# A word of at least this many letters is a content word.
_CONTENT_MIN_LEN = 4


def _fold_tokens(text: str) -> list[str]:
    return speech_tokens(str(text or "").replace("’", "'"))


def _within_edits(a: str, b: str, limit: int) -> bool:
    """Is the edit distance between ``a`` and ``b`` at most ``limit``? (stops early)"""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ch in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ch != other))
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


class _Reference:
    """The reference's folded words, indexed to align each pass to it in order."""

    def __init__(self, text: str) -> None:
        self.tokens = _fold_tokens(text)
        self._exact: dict[str, list[int]] = defaultdict(list)
        for index, token in enumerate(self.tokens):
            self._exact[token].append(index)
        self._long: dict[tuple[str, int], list[str]] = defaultdict(list)
        for token in self._exact:
            if len(token) >= _FUZZY_MIN_LEN:
                self._long[token[:_FUZZY_PREFIX], len(token)].append(token)
        self._close_cache: dict[tuple[str, str], bool] = {}

    def _close(self, a: str, b: str) -> bool:
        key = (a, b)
        cached = self._close_cache.get(key)
        if cached is None:
            limit = max(1, max(len(a), len(b)) // _FUZZY_EDITS_PER_LETTERS)
            cached = self._close_cache[key] = _within_edits(a, b, limit)
        return cached

    def _positions(self, token: str, expected: float) -> list[int]:
        found = list(self._exact.get(token, ()))
        if len(token) >= _FUZZY_MIN_LEN:
            reach = max(1, len(token) // _FUZZY_EDITS_PER_LETTERS) + 1
            for length in range(len(token) - reach, len(token) + reach + 1):
                for other in self._long.get((token[:_FUZZY_PREFIX], length), ()):
                    if other != token and self._close(token, other):
                        found.extend(self._exact[other])
        if len(found) > _MAX_OCCURRENCES:
            found = sorted(found, key=lambda index: abs(index - expected))[:_MAX_OCCURRENCES]
        return sorted(found)

    def heard(self, tokens: Sequence[str]) -> list[bool]:
        """Which of ``tokens`` sit, in order, where the reference has them.

        The longest common subsequence under exact-or-close matching (Hunt-Szymanski), so a
        word counts only if the words before and after it also line up.
        """
        scale = len(self.tokens) / max(len(tokens), 1)
        thresholds: list[int] = []
        chains: list[tuple[int, int, Any]] = []
        for j, token in enumerate(tokens):
            for i in reversed(self._positions(token, j * scale)):
                k = bisect.bisect_left(thresholds, i)
                node = (j, i, chains[k - 1] if k else None)
                if k == len(thresholds):
                    thresholds.append(i)
                    chains.append(node)
                else:
                    thresholds[k] = i
                    chains[k] = node
        flags = [False] * len(tokens)
        node = chains[-1] if chains else None
        while node is not None:
            flags[node[0]] = True
            node = node[2]
        return flags


# -- 3. which pass wins which stretch? -----------------------------------------------------------

# Two stretches may overlap by this much (whisper's word edges are coarse) and still both be kept.
_OVERLAP_TOLERANCE_S = 0.2


@dataclass(frozen=True)
class MixedPass:
    """One transcription pass: the language it reports and its timed words
    (``text``/``start_s``/``end_s`` and any extra keys, kept as they are)."""

    language: str
    words: Sequence[dict[str, Any]]


@dataclass(frozen=True)
class MixedMerge:
    """The words to store, in time order, and which language each stretch is in."""

    words: tuple[dict[str, Any], ...]
    language: str
    segments: tuple[dict[str, Any], ...]


@dataclass
class _Group:
    pass_index: int
    language: str
    words: list[dict[str, Any]]
    start_s: float
    end_s: float
    heard: int = 0
    length: int = 0
    content_heard: int = 0

    @property
    def score(self) -> int:
        # A heard word counts for it, an unheard one against it: of two readings of one
        # stretch, the one the reference confirms word for word wins.
        return 2 * self.heard - (self.length - self.heard)

    @property
    def confirmed(self) -> bool:
        return (
            self.heard >= 1
            and self.heard / self.length >= MIN_HEARD_SHARE
            and (self.heard >= 2 or self.content_heard >= 1)
            and self.score > 0
        )


def _groups(index: int, mixed: MixedPass, reference: _Reference, words: list[dict]) -> list[_Group]:
    owners: list[int] = []
    tokens: list[str] = []
    for position, word in enumerate(words):
        for token in _fold_tokens(word["text"]):
            tokens.append(token)
            owners.append(position)
    flags = reference.heard(tokens)
    heard_by_word: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for owner, token, flag in zip(owners, tokens, flags, strict=True):
        heard_by_word[owner].append((token, flag))
    positions = {id(word): position for position, word in enumerate(words)}
    groups: list[_Group] = []
    for rows in sentence_groups(words):
        group = _Group(
            pass_index=index,
            language=mixed.language,
            words=rows,
            start_s=float(rows[0]["start_s"]),
            end_s=max(float(row["end_s"]) for row in rows),
        )
        for row in rows:
            for token, flag in heard_by_word.get(positions[id(row)], ()):
                group.length += 1
                if flag:
                    group.heard += 1
                    group.content_heard += len(token) >= _CONTENT_MIN_LEN
        if group.length:
            groups.append(group)
    return groups


def _best_set(candidates: list[_Group]) -> list[_Group]:
    """Weighted interval scheduling: the non-overlapping groups of the greatest total score,
    ties to the one with more of the first pass in it."""
    order = sorted(candidates, key=lambda g: (g.end_s, g.start_s, g.pass_index))
    ends = [g.end_s for g in order]
    best: list[tuple[int, int]] = [(0, 0)]
    previous: list[int] = []
    taken: list[bool] = []
    for k, group in enumerate(order):
        p = bisect.bisect_right(ends, group.start_s + _OVERLAP_TOLERANCE_S, 0, k)
        with_group = (
            best[p][0] + group.score,
            best[p][1] + (group.score if group.pass_index == 0 else 0),
        )
        if with_group > best[k]:
            best.append(with_group)
            taken.append(True)
            previous.append(p)
        else:
            best.append(best[k])
            taken.append(False)
            previous.append(-1)
    chosen: list[_Group] = []
    k = len(order)
    while k > 0:
        if taken[k - 1]:
            chosen.append(order[k - 1])
            k = previous[k - 1]
        else:
            k -= 1
    return chosen[::-1]


def _touches(group: _Group, other: _Group) -> bool:
    """Do two stretches overlap, give or take the word-edge tolerance? (A zero-length word
    at 2.0 s touches the stretches ending and starting there.)"""
    return (
        group.start_s < other.end_s + _OVERLAP_TOLERANCE_S
        and other.start_s < group.end_s + _OVERLAP_TOLERANCE_S
    )


def merge_passes(
    first: MixedPass,
    extras: Sequence[MixedPass],
    reference_text: str,
    *,
    duration_s: float,
    covers_s: float | None = None,
) -> MixedMerge | None:
    """The first pass, with each stretch another pass has confirmed better taken from that pass.

    ``extras`` are passes in other languages over the same audio. The output is the stretches the
    reference confirms (``confirmed`` in ``segments``) plus every first-pass stretch that no
    confirmed extra-pass stretch touches: the first pass stands wherever nothing replaces it.
    ``covers_s`` is how much of the take the reference heard (None: all of it); no extra-pass
    stretch starting at or after it is a candidate. Returns None when no extra pass confirms
    anything the first pass does not (the caller then keeps the first pass as it is).
    """
    reference = _Reference(reference_text)
    if not reference.tokens:
        return None
    passes = [MixedPass(first.language, [dict(w) for w in first.words])]
    for extra in extras:
        # Forcing a language onto speech in another makes whisper loop or print a placeholder.
        passes.append(MixedPass(extra.language, clean_words(extra.words, duration_s=duration_s)))
    candidates: list[_Group] = []
    first_groups: list[_Group] = []
    for index, mixed in enumerate(passes):
        for group in _groups(index, mixed, reference, list(mixed.words)):
            if index == 0:
                first_groups.append(group)
            if group.confirmed and (covers_s is None or group.start_s < covers_s):
                candidates.append(group)
    chosen = _best_set(candidates)
    replacing = [group for group in chosen if group.pass_index > 0]
    if not replacing:
        return None
    chosen_ids = {id(group) for group in chosen}
    kept_first = [
        group
        for group in first_groups
        if id(group) not in chosen_ids and not any(_touches(group, other) for other in replacing)
    ]
    groups = sorted([*chosen, *kept_first], key=lambda g: (g.start_s, g.end_s, g.pass_index))

    # Words past the take are dropped or clipped (they would break the snapshot's window check);
    # in time order each word ends where the next begins at the latest.
    rows = [
        (dict(word), position)
        for position, group in enumerate(groups)
        for word in group.words
        if 0.0 <= float(word["start_s"]) < duration_s
    ]
    rows.sort(key=lambda item: (float(item[0]["start_s"]), float(item[0]["end_s"])))
    for index, (word, _position) in enumerate(rows):
        next_start = float(rows[index + 1][0]["start_s"]) if index + 1 < len(rows) else duration_s
        limit = min(next_start, duration_s)
        word["end_s"] = max(float(word["start_s"]), min(float(word["end_s"]), limit))
    extent: dict[int, tuple[float, float]] = {}
    for word, position in rows:
        low, high = extent.get(position, (float(word["start_s"]), float(word["end_s"])))
        extent[position] = (min(low, float(word["start_s"])), max(high, float(word["end_s"])))

    mass: dict[str, int] = {}
    segments: list[dict[str, Any]] = []
    for position, group in enumerate(groups):
        if position not in extent:
            continue
        start, end = extent[position]
        confirmed = id(group) in chosen_ids
        if confirmed:
            mass[group.language] = mass.get(group.language, 0) + group.heard
        last = segments[-1] if segments else None
        if last and last["language"] == group.language and last["confirmed"] == confirmed:
            last["end_s"] = round(max(end, last["end_s"]), 3)
        else:
            segments.append(
                {
                    "start_s": round(start, 3),
                    "end_s": round(end, 3),
                    "language": group.language,
                    "confirmed": confirmed,
                }
            )
    return MixedMerge(
        words=tuple(word for word, _ in rows),
        language=max(mass, key=mass.__getitem__, default=first.language),
        segments=tuple(segments),
    )


__all__ = [
    "LEXICONS",
    "MAX_EXTRA_PASSES",
    "MixedMerge",
    "MixedPass",
    "merge_passes",
    "reference_languages",
]
