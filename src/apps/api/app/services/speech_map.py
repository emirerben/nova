"""speech_map — compact spoken-word + pause map for editor/copilot consumers.

Derives, from a variant's persisted word records (see
`transcript_source.speech_words_for_variant`), the two timing vocabularies the
edit copilot needs for speech-synced placement:

  words:  [{"w": str, "s": float, "e": float}]   spoken words, assembled-timeline s
  pauses: [{"s": float, "e": float, "after": str|None}]  silences between words

Coordinate invariant: every time is FINAL assembled-timeline seconds. Words
that fall outside [0, duration + epsilon] are dropped (a wrong-coordinate
source would otherwise place SFX at nonsense times); end < start or non-finite
drops the word. Pure arithmetic — safe to run on every status poll.
"""

from __future__ import annotations

import math

PAUSE_MIN_GAP_S = 0.28
LEADING_PAUSE_MIN_S = 0.5
# Matches every consumer's cap (snapshot.ts COPILOT_SPEECH_WORDS_MAX and
# edit_copilot.py _SPEECH_WORDS_SHOWN_MAX) — larger would be pure wire weight.
WORDS_CAP = 150
PAUSES_CAP = 40
_WORD_MAX_CHARS = 40
_COORD_EPSILON_S = 0.75


def build_speech_map(
    words: list[dict] | None,
    duration_s: float | None,
    source: str,
) -> dict | None:
    """Return the compact speech map, or None when no usable words survive.

    `words` are transcript_source-shaped records ({"word","start_s","end_s"}).
    Head-biased caps: early words matter most (hook-window asks like "the first
    4 seconds"), so overflow drops from the tail, never strided.
    """
    clean: list[dict] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        text = str(w.get("word") or w.get("text") or "").strip()
        if not text:
            continue
        try:
            start_s = float(w.get("start_s", 0.0))
            end_s = float(w.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start_s) or not math.isfinite(end_s):
            continue
        if start_s < 0.0 or end_s < start_s:
            continue
        if duration_s is not None and start_s > float(duration_s) + _COORD_EPSILON_S:
            continue
        clean.append({"w": text[:_WORD_MAX_CHARS], "s": round(start_s, 2), "e": round(end_s, 2)})
    if not clean:
        return None
    clean.sort(key=lambda x: (x["s"], x["e"]))
    pauses: list[dict] = []
    if clean[0]["s"] >= LEADING_PAUSE_MIN_S:
        pauses.append({"s": 0.0, "e": clean[0]["s"], "after": None})
    for a, b in zip(clean, clean[1:]):
        gap = b["s"] - a["e"]
        if gap >= PAUSE_MIN_GAP_S:
            pauses.append({"s": a["e"], "e": b["s"], "after": a["w"]})
    # Trailing silence: a punchline that ends the speech still needs a pause
    # mark for its accent ("the accent lands at the pause right after it").
    if duration_s is not None:
        tail_gap = float(duration_s) - clean[-1]["e"]
        if tail_gap >= PAUSE_MIN_GAP_S:
            pauses.append(
                {"s": clean[-1]["e"], "e": round(float(duration_s), 2), "after": clean[-1]["w"]}
            )
    return {
        "source": source,
        "words": clean[:WORDS_CAP],
        "pauses": pauses[:PAUSES_CAP],
    }
