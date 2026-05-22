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

# Maximum gap between two atomized same-text phrases that still counts as
# OCR re-detection of the same on-screen word. Phrases farther apart than
# this are kept separate because they likely represent a real repeat
# (e.g. a refrain or a beat-synced re-display) rather than the multi-frame
# OCR fan-out that produced job 673d26d7-edbf-43a8-ac58-50dd604baae0 's
# "and / and / and / and / and" stutter on template Not Just Luck. Tuned
# to be a touch above sub-second neighbor sampling without swallowing a
# typical word's display duration.
ATOMIZED_DEDUP_GAP_THRESHOLD_S = 0.5

# Single characters the artifact filter keeps when they appear as standalone
# atomized OCR phrases. Everything else single-char is treated as noise.
#
#  - {i, a, o} — one-letter English words. Casefolded for matching.
#  - {0..9} — counter/step digits ("DAY 1", "ROUND 2", or animated step
#    counters where each digit is its own atomized phrase). A standalone
#    digit is more often legitimate text than OCR noise; the artifact
#    pattern we're targeting (prod template 89cde014, the spurious "W"
#    with confidence 0.634) is single ALPHABETIC chars outside the
#    one-letter-word set, not digits.
#
# Latin-script-biased: a single CJK / Arabic / Hebrew character would
# satisfy `isalnum()` and fail this whitelist, so it currently drops. For
# non-English templates, revisit — probably extend with
# `unicodedata.category(ch) == 'Lo'` (Letter, other) or similar.
_ATOMIZED_SINGLE_CHAR_WHITELIST = frozenset(
    {"i", "a", "o", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}
)


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
    re-merged into a multi-line phrase. After per-event finalize, the
    output passes through `dedup_overlapping_atomized_phrases` so the
    same on-screen word captured by OCR across multiple frame samples
    collapses back to a single phrase covering the union of its
    appearances.
    """
    if not events:
        _emit_stage_d_summary(events_in=0, phrases_out=0, atomized=atomize_per_event)
        return []

    if atomize_per_event:
        sorted_events = sorted(events, key=lambda e: (e.start_t_s, e.aabb[1]))
        finalized = [_finalize([ev]) for ev in sorted_events]
        # Filter OCR artifacts BEFORE dedup so dedup-key counts (and the
        # downstream LineGroup builder) operate on clean text. See
        # `_is_atomized_ocr_artifact` for the rule set.
        pre_filter = len(finalized)
        finalized = [p for p in finalized if not _is_atomized_ocr_artifact(p)]
        artifacts_dropped = pre_filter - len(finalized)
        dedup_in = len(finalized)
        out = dedup_overlapping_atomized_phrases(
            finalized, gap_threshold_s=ATOMIZED_DEDUP_GAP_THRESHOLD_S
        )
        _emit_stage_d_summary(
            events_in=len(events),
            phrases_out=len(out),
            atomized=True,
            dedup_collapsed=dedup_in - len(out),
            artifacts_dropped=artifacts_dropped,
        )
        return out

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

    out = [
        _finalize(p_events)
        for p_events in sorted(open_phrases, key=lambda pe: min(e.start_t_s for e in pe))
    ]
    _emit_stage_d_summary(
        events_in=len(events),
        phrases_out=len(out),
        atomized=False,
    )
    return out


def _emit_stage_d_summary(
    *,
    events_in: int,
    phrases_out: int,
    atomized: bool,
    dedup_collapsed: int = 0,
    artifacts_dropped: int = 0,
) -> None:
    """Best-effort pipeline_trace event so /admin/jobs Debug tab can show
    Stage D's events-in vs phrases-out counts (atomized-dedup collapses and
    OCR-artifact drops in atomized mode). Silently no-ops when there's no
    active job context."""
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "overlay",
            "stage_d_summary",
            {
                "events_in": events_in,
                "phrases_out": phrases_out,
                "atomized": atomized,
                "dedup_collapsed": dedup_collapsed,
                "artifacts_dropped": artifacts_dropped,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _is_atomized_ocr_artifact(phrase: Phrase) -> bool:
    """Return True when an atomized one-word phrase is OCR noise that should
    not survive into Stage E or Stage G's cumulative-reveal grouping.

    Three rules — any one fires:
      1. **Pure non-alphanumeric** — token has zero alphanumeric chars (e.g.
         stray `"`, `--`, `…`). These are punctuation-only OCR hits.
      2. **Single character outside the English one-letter whitelist** —
         a single alphanumeric not in {I, A, O, a, i, o}. Real one-letter
         words ("I am", "a cat") survive; OCR noise like "W" / "M" / "8"
         drops. Whitelisted by casefold.
      3. **Punctuation-dominant** — more than half the characters are
         non-alphanumeric (token shape `..!?` or `,,,` etc.).

    Multi-character real words with stray trailing punctuation (e.g.
    `luck"`) are NOT dropped here — they're legitimate text with OCR noise
    around the edges. Stage E's transcript alignment normalizes those.

    Atomized-only: this function assumes ``phrase.lines == [<one word>]``.
    Multi-line phrases (build-up captions) bypass artifact filtering and
    go straight through Stage E.
    """
    if len(phrase.lines) != 1:
        return False
    text = phrase.lines[0].strip()
    if not text:
        # Empty/whitespace-only token is itself an artifact.
        return True
    alnum_count = sum(1 for ch in text if ch.isalnum())
    if alnum_count == 0:
        return True
    if len(text) == 1 and text.casefold() not in _ATOMIZED_SINGLE_CHAR_WHITELIST:
        return True
    if alnum_count * 2 < len(text):
        # More than half punctuation: e.g. "x.." or ",,a" — noise, not text.
        return True
    return False


def _finalize(phrase_events: list[TextEvent]) -> Phrase:
    """Build an immutable `Phrase` from a cluster of events.

    Lines are sorted by Y-center top-to-bottom so the resulting
    `sample_text` reads as it does on screen. Phrase timing spans from
    the earliest event start to the latest event end. The bbox is the
    union of all constituent event bboxes. Confidence is the mean of
    constituent event mean-confidences (event-weighted, not frame-weighted
    — phrases with one long-lived event and one brief event are not
    skewed by the long-lived one's higher frame count).

    Same-text events are deduplicated by casefolded-stripped key. An
    identical word appearing in two events within one phrase is always
    OCR re-detection (bbox drifted, frame sampling captured it twice) —
    never an intentional repeat in viral-template captions. Without this
    dedup, a single phrase with lines=["if", "you", "if", "you", "put",
    "put", "in"] would render as the literal duplicated string visible
    in prod job 87b7292b-3913-40b9-844f-835f7544063b.
    """
    y_sorted = sorted(phrase_events, key=lambda e: e.y_center())
    seen: set[str] = set()
    lines: list[str] = []
    for ev in y_sorted:
        key = ev.text.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(ev.text)
    if not lines:
        lines = [y_sorted[0].text]
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


def dedup_overlapping_atomized_phrases(
    phrases: list[Phrase],
    *,
    gap_threshold_s: float,
) -> list[Phrase]:
    """Collapse same-text phrases whose time intervals overlap or sit within
    `gap_threshold_s` of each other.

    Reused from two call sites: (1) the tail of `reconstruct_phrases` in
    atomized mode — kills OCR re-detection duplicates BEFORE alignment; and
    (2) `pipeline.run_full_pipeline` after Stage E (text_alignment) — kills
    duplicates the LLM CREATES when transcript is shorter than the OCR
    phrase set and multiple distinct OCR phrases get assigned the same
    transcript word. Stage-D dedup alone is not enough because Stage E
    introduces new collisions on a clean input.

    Concrete failure mode (prod job 673d26d7-edbf-43a8-ac58-50dd604baae0
    on template Not Just Luck, 2026-05-20): atomized output contained
    "the" twice (overlapping intervals at 4.5-5.5 and 5.0-6.0), "to" three
    times all at 5.5-6.0, "combination" three times spanning 8.0-9.5, and
    "and" five times in the 9.5-10.5 window. Renderer stacked all 21
    overlays center-positioned on top of each other.

    Second failure mode (prod template fdaf3bbc reanalyze 2026-05-21,
    AFTER the above fix): 31 clean Stage-D phrases + 15 transcript words →
    text_alignment emitted "allow" 3× at 9.5-10.0 (with "anyone" and
    "diminish" both relabeled to "allow"), plus "combination" 4× and
    "anyone" 4×. Stage-D dedup ran on a clean input so the duplicates
    only appeared post-alignment.

    Algorithm: group by casefolded-stripped text key, sort each group by
    start time, then merge intervals where the next start is within
    `gap_threshold_s` of the prior end. Order is restored at the end by
    re-sorting on start time so the renderer still walks the timeline.

    `gap_threshold_s` is tuned to swallow OCR re-detection (sub-second
    frame sampling) without swallowing legitimate repeats — a beat-synced
    refrain or a deliberate re-display will have a gap larger than this.
    A separate `gap_threshold_s=0` would only collapse strict overlaps
    and miss the sub-second neighbor cases.
    """
    if len(phrases) <= 1:
        return list(phrases)

    groups: dict[str, list[Phrase]] = {}
    for p in phrases:
        key = "\n".join(line.strip().casefold() for line in p.lines)
        groups.setdefault(key, []).append(p)

    merged: list[Phrase] = []
    for key, group in groups.items():
        group.sort(key=lambda p: p.start_t_s)
        run: Phrase = group[0]
        for p in group[1:]:
            if p.start_t_s - run.end_t_s <= gap_threshold_s:
                run = Phrase(
                    lines=run.lines,
                    start_t_s=run.start_t_s,
                    end_t_s=max(run.end_t_s, p.end_t_s),
                    aabb=_aabb_union(run.aabb, p.aabb),
                    # Pick the higher confidence so a marginal re-detection
                    # next to a strong one doesn't drag the score down.
                    mean_confidence=max(run.mean_confidence, p.mean_confidence),
                )
            else:
                merged.append(run)
                run = p
        merged.append(run)

    merged.sort(key=lambda p: p.start_t_s)
    return merged
