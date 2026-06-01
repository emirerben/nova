"""Synthesize word-reveal timings for *generated* overlay text.

Lyric overlays (`lyric_injector._inject_karaoke`) have real per-word timings from
Whisper alignment, so they emit aligned `start_s`/`end_s` plus a centisecond
`duration_cs` fallback. Generated text (the generative-edit
intro overlay) has NO spoken timing — the words were written by an agent, not sung —
so we synthesize an even split across the overlay window, optionally snapping word
boundaries to musical beats when a song is present.

Both paths emit the SAME contract the renderer reads
(`text_overlay_skia._draw_karaoke_line`): a list of
`{text, start_s, end_s, duration_cs}`. The renderer switches each word to the
highlight color at `start_s`; `duration_cs` is retained as a fallback payload for
legacy callers and ASS.

`MIN_WORD_CS` mirrors the floor in `lyric_injector` so a synthesized sweep can never
emit a sub-renderable (<50 ms) word that would flicker or collapse onto its neighbour.

Guarantees (the correctness contract — see tests/pipeline/test_word_timing.py):
  - word starts/ends are STRICTLY increasing (no two words highlight at once),
  - every `duration_cs >= MIN_WORD_CS`,
  - beat-snapping only ever moves a word-end onto an in-window beat, never backwards
    past the previous word and never onto a beat already claimed by the previous word
    (a collapse falls back to the even-split position for that word).
"""

from __future__ import annotations

# Centisecond floor per word. Matches `lyric_injector._MIN_OVERLAY_DURATION_S`-class
# reasoning: ASS/Skia rendering of <50 ms reveals flickers, and a 0-length gap would
# make two words flip to the highlight color on the same frame.
MIN_WORD_CS = 5
MIN_WORD_S = MIN_WORD_CS / 100.0


def synthesize_word_timings(
    words: list[str],
    start_s: float,
    end_s: float,
    beats: list[float] | None = None,
) -> list[dict]:
    """Build `word_timings` for an overlay spanning [start_s, end_s].

    Args:
        words: the overlay's words, in order. Empty/whitespace tokens are dropped.
        start_s, end_s: the overlay window, in whatever coordinate the caller uses
            (slot-relative is typical). Only the *width* matters for the even split.
        beats: optional beat timestamps in the SAME coordinate as start_s/end_s.
            Beats inside the window pull word-ends toward the rhythm; beats outside
            the window are ignored. Pass None (or []) for the no-music path.

    Returns:
        list of {text, start_s, end_s, duration_cs}, overlay-relative. `[]` when there
        is nothing renderable (no words, or a non-positive window).
    """
    toks = [str(w).strip() for w in (words or []) if str(w).strip()]
    if not toks:
        return []

    window = float(end_s) - float(start_s)
    if window <= 0:
        return []

    n = len(toks)
    per = window / n

    # Beats rebased to overlay-relative time, kept only if strictly inside the window.
    rel_beats: list[float] = []
    if beats:
        for b in beats:
            try:
                rb = float(b) - float(start_s)
            except (TypeError, ValueError):
                continue
            if 0.0 < rb < window:
                rel_beats.append(rb)
        rel_beats.sort()

    ends: list[float] = []
    prev = 0.0
    last_beat: float | None = None
    for i in range(n):
        # Even-split target for this word's end (last word lands at the window edge).
        target = min((i + 1) * per, window)
        end = target

        if rel_beats:
            nearest = min(rel_beats, key=lambda b: abs(b - target))
            # Only snap if it moves the boundary forward past the previous word and
            # onto a beat the previous word didn't already claim. Otherwise two words
            # would collapse onto one instant — keep the even-split position instead.
            if nearest > prev + MIN_WORD_S and nearest != last_beat:
                end = nearest
                last_beat = nearest

        # Monotonicity + min-gap floor. Deliberately NOT capped at `window`: if the
        # floor pushes a tail word past the overlay end, that word simply never
        # reaches the highlight state at render time (t_local maxes at duration_s),
        # which is graceful — far better than two words sharing an end timestamp.
        end = max(end, prev + MIN_WORD_S)
        ends.append(end)
        prev = end

    out: list[dict] = []
    prev_end = 0.0
    for tok, end in zip(toks, ends):
        duration_cs = max(MIN_WORD_CS, int(round((end - prev_end) * 100)))
        out.append(
            {
                "text": tok,
                "start_s": round(prev_end, 3),
                "end_s": round(end, 3),
                "duration_cs": duration_cs,
            }
        )
        prev_end = end
    return out
