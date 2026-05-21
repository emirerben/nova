"""Cumulative word-by-word reveal stage builder.

Pure transformation: a list of timed words + a line-end time → a list of
cumulative reveal stages. Each stage's `text` is the cumulative line built up
to and including that word. Stages are butted edge-to-edge so exactly one
stage is visible at any instant — the screen never holds two stacked overlays.

The algorithm is extracted verbatim from the music-lyric injector
(`lyric_injector._inject_per_word_pop`) so both pipelines share one
implementation. Behavior is locked by `tests/pipeline/test_lyric_injector.py`
(regression on the injector wrapper) and `tests/pipeline/test_text_reveal.py`
(direct unit tests on this helper).

Why the algorithm looks like it does:

- Middle stages whose natural span (`words[i+1].start_s - words[i].start_s`)
  is below `MIN_RENDERABLE_S` are DROPPED, not floor-clamped. Forcing a floor
  would extend an overlay past the next word's start, stacking two overlays
  for a few frames and producing a visible glitch. The dropped word still
  appears in the next surviving stage's cumulative text — no information is
  lost on screen, only the dedicated reveal moment for that word.
- The last word ALWAYS survives. A line whose terminal stage is dropped is
  meaningless — the viewer never sees the complete sentence.
- The last stage extends past `line_end_s` by `LAST_WORD_DWELL_S` so the
  complete line settles in the viewer's eye before clearing. Intentionally
  short — too long crowds the next line.
"""

from __future__ import annotations

from dataclasses import dataclass

LAST_WORD_DWELL_S = 0.30
MIN_RENDERABLE_S = 0.05


@dataclass(frozen=True, slots=True)
class Word:
    """One timed word. `end_s` is informational; only `start_s` drives stage
    boundaries (next stage starts at next word's start_s)."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class CumulativeStage:
    """One reveal stage.

    `text`           — cumulative line built up to and including this word.
    `start_s`, `end_s` — visibility window; butted to neighbors.
    `pop_animated_suffix` — the word added at this stage. The renderer's
        pop-in effect uses this to animate only the new tail while the prefix
        renders statically; without it the whole line would re-scale on each
        new word and the viewer would see the line flickering.
    """

    text: str
    start_s: float
    end_s: float
    pop_animated_suffix: str


def build_cumulative_stages(
    words: list[Word],
    line_end_s: float,
    *,
    dwell_s: float = LAST_WORD_DWELL_S,
    min_renderable_s: float = MIN_RENDERABLE_S,
) -> list[CumulativeStage]:
    """Build N cumulative reveal stages from N timed words.

    Two-pass:
      Pass 1 — decide which word indices survive `min_renderable_s` based on
               their NATURAL spans. The last word is forced to survive.
      Pass 2 — emit a stage per surviving word with `end_s` set to the NEXT
               surviving stage's `start_s` (not the immediate next word's),
               so dropped middle stages don't leave a sub-frame gap.

    Empty input → empty output. Caller is responsible for filtering
    whitespace-only words before calling.
    """
    if not words:
        return []

    # Pass 1: natural ends and keep mask.
    natural_ends: list[float] = [
        line_end_s if i == len(words) - 1 else words[i + 1].start_s for i in range(len(words))
    ]
    keep_mask: list[bool] = [
        (natural_ends[i] - words[i].start_s) >= min_renderable_s for i in range(len(words))
    ]
    # Terminal stage must survive; without it the cumulative line never
    # completes. If its natural span is short, the dwell extension below
    # pads it to renderable.
    keep_mask[-1] = True

    # Pre-compute "next kept stage's word.start_s" walking backward; O(n).
    next_kept_start: list[float | None] = [None] * len(words)
    next_start: float | None = None
    for i in range(len(words) - 1, -1, -1):
        if keep_mask[i]:
            next_kept_start[i] = next_start  # None marks the last kept stage
            next_start = words[i].start_s

    # Pass 2: emit stages.
    stages: list[CumulativeStage] = []
    for i, word in enumerate(words):
        if not keep_mask[i]:
            continue
        cumulative = " ".join(w.text.strip() for w in words[: i + 1] if w.text.strip())
        if not cumulative:
            continue
        if next_kept_start[i] is None:
            end_s = line_end_s + dwell_s
        else:
            end_s = next_kept_start[i]
        # Defensive: a terminal stage whose start_s is somehow past line_end_s
        # would emit end_s <= start_s. Guard with min_renderable_s so the
        # caller can still validate the window.
        if end_s - word.start_s < min_renderable_s:
            end_s = word.start_s + min_renderable_s
        stages.append(
            CumulativeStage(
                text=cumulative,
                start_s=round(word.start_s, 3),
                end_s=round(end_s, 3),
                pop_animated_suffix=word.text.strip(),
            )
        )
    return stages
