"""Stage D: phrase reconstruction — `TextEvent[]` to `Phrase[]`.

A "phrase" is a cluster of `TextEvent`s that visually belong to the same
caption. Three cases this stage handles:

1. Single line: one event, one phrase, one line.
2. Simultaneous multi-line: events that start at the same time at different
   Y positions but share an X-band (e.g. a centered two-line headline).
3. Build-up / typewriter captions: events appearing sequentially, each one
   starting before the prior events finish. The phrase grows as words
   accumulate; finalize when the screen clears.

A new phrase starts when no event from any prior phrase is visible at the
moment the new event begins. That is the "screen clear" signal — and it is
exactly what the existing Gemini-only agent fails to detect when it
conflates separate phrases into one.

Two events join the same phrase iff BOTH conditions hold:

- Time overlap: the new event starts while at least one event already in
  the phrase is still on screen.
- Spatial proximity: the new event's X-center is within `x_band_threshold`
  of the phrase's mean X-center. This keeps a persistent corner watermark
  from being merged into a center hook that happens to overlap in time.

Lines within a phrase are sorted top-to-bottom by Y-center, so `sample_text`
matches what a human sees when the phrase is fully assembled.

Pure function — no I/O, no model calls. Fully deterministic for fixture-
based testing.
"""

from __future__ import annotations

from app.agents._schemas.text_overlay_pipeline import (
    Phrase,
    TextEvent,
    _aabb_union,
)

DEFAULT_X_BAND_THRESHOLD = 0.15


def reconstruct_phrases(
    events: list[TextEvent],
    *,
    x_band_threshold: float = DEFAULT_X_BAND_THRESHOLD,
    atomize_per_event: bool = False,
) -> list[Phrase]:
    """Cluster `TextEvent`s into visually coherent phrases.

    Walks events in start-time order. For each event, finds the
    already-open phrase whose latest-ending event is still visible at the
    new event's start, AND whose mean X-center is within
    `x_band_threshold` of the new event's X-center. If a match exists,
    the event joins that phrase; otherwise it starts a new phrase.

    Multiple eligible phrases are broken by picking the spatially-closest
    one. This matters when two phrases briefly overlap in time but at
    different X positions — each event lands in its own.

    `atomize_per_event=True` skips clustering entirely — each input event
    becomes its own one-line phrase. Used by the word-by-word pipeline
    (after `tokenize_detections_into_words`) where each event already
    represents a single word and same-line same-time words must NOT be
    re-merged into a multi-line phrase.
    """
    if not events:
        return []

    if atomize_per_event:
        return [
            _finalize([ev])
            for ev in sorted(events, key=lambda e: (e.start_t_s, e.aabb[1]))
        ]

    events_sorted = sorted(events, key=lambda e: (e.start_t_s, e.aabb[1]))
    open_phrases: list[list[TextEvent]] = []

    for ev in events_sorted:
        best_idx: int | None = None
        best_distance = float("inf")
        for i, phrase_events in enumerate(open_phrases):
            latest_end = max(p.end_t_s for p in phrase_events)
            if ev.start_t_s > latest_end:
                continue
            mean_x = sum(p.x_center() for p in phrase_events) / len(phrase_events)
            distance = abs(ev.x_center() - mean_x)
            if distance <= x_band_threshold and distance < best_distance:
                best_distance = distance
                best_idx = i

        if best_idx is not None:
            open_phrases[best_idx].append(ev)
        else:
            open_phrases.append([ev])

    return [
        _finalize(p_events)
        for p_events in sorted(open_phrases, key=lambda pe: min(e.start_t_s for e in pe))
    ]


def _finalize(phrase_events: list[TextEvent]) -> Phrase:
    """Build an immutable `Phrase` from a cluster of events.

    Lines are sorted by Y-center top-to-bottom so the resulting
    `sample_text` reads as it does on screen. Phrase timing spans from
    the earliest event start to the latest event end. The bbox is the
    union of all constituent event bboxes. Confidence is the mean of
    constituent event mean-confidences (event-weighted, not frame-weighted
    — phrases with one long-lived event and one brief event are not
    skewed by the long-lived one's higher frame count).
    """
    y_sorted = sorted(phrase_events, key=lambda e: e.y_center())
    lines = [e.text for e in y_sorted]
    start_t_s = min(e.start_t_s for e in phrase_events)
    end_t_s = max(e.end_t_s for e in phrase_events)
    aabb = phrase_events[0].aabb
    for e in phrase_events[1:]:
        aabb = _aabb_union(aabb, e.aabb)
    mean_confidence = sum(e.mean_confidence for e in phrase_events) / len(phrase_events)
    return Phrase(
        lines=lines,
        start_t_s=start_t_s,
        end_t_s=end_t_s,
        aabb=aabb,
        mean_confidence=mean_confidence,
    )
