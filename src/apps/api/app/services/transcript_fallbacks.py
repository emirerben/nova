"""Heuristic, LLM-free fallbacks for the "Get a transcript" flow.

Used when Gemini is unavailable (no GEMINI_API_KEY — the local dev machine) or when
an agent call fails, so the flow still works end-to-end at localhost. These are
deliberately plain: a small fixed question bank and a templated script sized to the
footage. Never great, always non-empty and band-valid (the script lands inside the
same spoken-length band the `script_writer` agent enforces), so the teleprompter
always has something to show.
"""

from __future__ import annotations

from app.schemas.voiceover_script import target_word_count

# A tiny fixed interview: at most 3 questions, each with tappable suggestions.
_QUESTION_BANK: list[tuple[str, list[str]]] = [
    (
        "What's the one thing you want people to feel watching this?",
        ["Calm", "Hyped", "Nostalgic", "Curious"],
    ),
    ("What's the tone — how should it sound?", ["Real and low-key", "Funny", "Warm", "Punchy"]),
    (
        "Anything specific you want to mention?",
        ["Not really", "A detail about the place", "Who it's for"],
    ),
]

_FILLER = (
    "It's one of those things you don't really notice until you slow down.",
    "But that's exactly the part worth showing.",
    "There's a quiet kind of beauty in it if you look closely.",
    "And honestly, it's the moment I'd want you to see.",
    "No filter, no script — just how it actually felt.",
)


def heuristic_question(turn_count: int) -> tuple[str, list[str], bool]:
    """Return (question, suggestions, is_final) for the given turn (0-based count of
    questions already asked). Closes after the bank is exhausted (<= 3 turns)."""
    idx = max(0, int(turn_count))
    if idx >= len(_QUESTION_BANK):
        # Past the bank — a light final question the user can skip.
        return ("Anything else before I write it?", ["Nope, write it"], True)
    question, suggestions = _QUESTION_BANK[idx]
    is_final = idx == len(_QUESTION_BANK) - 1
    return (question, suggestions, is_final)


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip()


# Hard ceiling for the heuristic path, mirroring the agent's _ABS_MAX_WORDS band.
# The floor loop below appends until it reaches `lo`; without this cap a bad
# client-supplied `duration_s` (the route trusts the body) would drive `lo` into
# the millions and the O(n) split-per-append becomes a quadratic request-thread DoS.
_MAX_HEURISTIC_WORDS = 260


def heuristic_script(brief: str, answers: list[str], duration_s: float) -> str:
    """A templated first-person script sized to the footage. Aims for the target
    word count and always lands within [0.7x, 1.4x] of it (the script_writer band),
    clamped to `_MAX_HEURISTIC_WORDS`, so the route can persist it without an agent
    round-trip and an absurd duration can't blow up the floor loop."""
    target = min(target_word_count(duration_s), _MAX_HEURISTIC_WORDS)
    lo = max(12, min(round(target * 0.7), _MAX_HEURISTIC_WORDS))
    hi = min(round(target * 1.4), _MAX_HEURISTIC_WORDS)

    subject = _clean(brief) or "this"
    sentences: list[str] = [f"So this is {subject}."]
    for a in answers:
        a = _clean(a)
        if a and a.lower() not in {"not really", "nope", "no", "nope, write it"}:
            sentences.append(f"{a[0].upper()}{a[1:]}." if a else "")
    sentences = [s for s in sentences if s]

    i = 0
    while len(" ".join(sentences).split()) < target and i < len(_FILLER) * 4:
        sentences.append(_FILLER[i % len(_FILLER)])
        i += 1

    # Trim back to the band at a sentence boundary if we overshot.
    while len(" ".join(sentences).split()) > hi and len(sentences) > 1:
        sentences.pop()

    text = " ".join(sentences).strip()
    # Guarantee the floor (pathological tiny targets) without breaking a sentence.
    while len(text.split()) < lo:
        text = f"{text} {_FILLER[len(text) % len(_FILLER)]}"
    return text
