"""Mid-sentence capitals in caption words that a second hearing does not have (Green 4/5).

whisper-1's timed words carry no case; punctuation restore (`transcribe.align_punctuated_text`)
copies the case of whisper's full text onto them, and that text sometimes title-cases ordinary
words ("a Sunrise Fashion Show at Pamukkale"). Gemini's clip record of the same take is an
independent hearing ("a Sunrise fashion show at Pamukale"). A capital that is not at a sentence
start is lowered only when that record writes the same word, mid-sentence, in lower case every
time it has it. Everything else stays as heard: sentence starts, "I", names both hearings
capitalise ("Sunrise", "Lumo"), and words the record does not have at all ("Pamukkale" vs its
"Pamukale"). Pure; no model call; words keep their timing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)]*$")
_ALWAYS_UPPER = {"i", "i'm", "i’m", "i've", "i’ve", "i'll", "i’ll", "i'd", "i’d"}


def _key(word: str) -> str:
    return word.casefold()


def reference_case(texts: Iterable[str | None]) -> dict[str, bool]:
    """Folded word -> True when the reference ever writes it capitalised mid-sentence, False when
    every mid-sentence use is lower case. Sentence starts say nothing about a word's case."""
    seen: dict[str, bool] = {}
    for text in texts:
        sentence_start = True
        for match in re.finditer(r"\S+", text or ""):
            token = match.group(0)
            word = _WORD.search(token)
            if word is not None and not sentence_start:
                letters = word.group(0)
                upper = letters[:1].isupper()
                seen[_key(letters)] = seen.get(_key(letters), False) or upper
            sentence_start = bool(_SENTENCE_END.search(token))
    return seen


def _lowered(token: str, reference: dict[str, bool], sentence_start: bool) -> str:
    word = _WORD.search(token)
    if word is None or sentence_start:
        return token
    letters = word.group(0)
    if (
        not letters[:1].isupper()
        or letters.isupper()
        or _key(letters) in _ALWAYS_UPPER
        or reference.get(_key(letters), True)
    ):
        return token
    return token[: word.start()] + letters[:1].lower() + letters[1:] + token[word.end() :]


def _lower_tokens(text: str, reference: dict[str, bool], sentence_start: bool) -> tuple[str, bool]:
    tokens = text.split(" ")
    out = []
    for token in tokens:
        out.append(_lowered(token, reference, sentence_start))
        if token.strip():
            sentence_start = bool(_SENTENCE_END.search(token.strip()))
    return " ".join(out), sentence_start


def settle_caption_case(
    cues: Sequence[dict[str, Any]], reference_texts: Iterable[str | None]
) -> list[dict[str, Any]]:
    """``cues`` with the unsupported mid-sentence capitals lowered (word and cue text alike).
    A cue with timed words is judged on the word stream, so a cue that starts mid-sentence
    ("a Sunrise Fashion Show…" after "…its 10th year with") is not taken for a sentence start.
    A touched cue is a copy; no reference, no change."""
    reference = reference_case(reference_texts)
    if not reference:
        return list(cues)
    out: list[dict[str, Any]] = []
    sentence_start = True
    for cue in cues:
        at_cue_start = sentence_start
        text, sentence_start = _lower_tokens(str(cue.get("text") or ""), reference, at_cue_start)
        changed = {} if text == cue.get("text") else {"text": text}
        words = cue.get("words")
        if isinstance(words, list) and words and all(isinstance(w, dict) for w in words):
            new_words, start = [], at_cue_start
            for word in words:
                heard = str(word.get("text") or "")
                lowered = _lowered(heard, reference, start)
                new_words.append(word if lowered == heard else {**word, "text": lowered})
                if heard.strip():
                    start = bool(_SENTENCE_END.search(heard.strip()))
            sentence_start = start
            if new_words != words:
                changed["words"] = new_words
        out.append({**cue, **changed} if changed else cue)
    return out
