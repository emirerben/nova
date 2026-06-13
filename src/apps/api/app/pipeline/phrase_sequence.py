"""Deterministic phrase engine for the transcript-synced typographic sequence.

The "editorial quote" treatment renders the spoken words of a video as kinetic
typography: the transcript is grouped into short phrase *scenes* that replace
each other on screen, synced to speech (reference aesthetic: TikTok quote
edits). This module is the pure timing/grouping half of that feature — no LLM,
no skia, no FFmpeg. Given Whisper words (``app/pipeline/transcribe.py``) and
the video duration, it decides:

1. Whether the speech is even worth rendering as a sequence
   (``speech_eligibility`` — D8): enough words (``MIN_SPEECH_WORDS``) AND the
   speech span must cover at least ``COVERAGE_MIN_FRAC`` of the timeline, so a
   two-second aside over a 60s montage never triggers the treatment.
2. How words group into phrases (``split_phrases``): terminal punctuation ends
   a phrase, a silence gap > ``PAUSE_GAP_S`` ends a phrase, and a phrase is
   hard-capped at ``MAX_PHRASE_WORDS`` words.
3. Each scene's display window (D5, REVISED): scenes are CLEAN CUTS. A scene
   normally ends ``SCENE_CLEAR_GAP_S`` BEFORE the next scene starts
   (``fade_out=False`` — hard cut, no fade), so there is a brief all-clear
   gap and scenes NEVER overlap. Degenerate back-to-back phrases shrink the
   gap to one frame (``MIN_SCENE_GAP_S``) instead of collapsing to a
   zero/negative display window. If the next phrase is more than
   ``HOLD_CAP_S`` of silence away, the scene fades out early
   (``speech_end_s + HOLD_CAP_S + FADE_OUT_S``, ``fade_out=True``) and the
   screen is allowed to be text-free until speech resumes. The last scene
   holds at most ``HOLD_CAP_S`` past its final word (fade starting no later
   than 0.05s before the video ends), then fades, clamped to the video
   duration. ``fade_out=True`` ONLY for hold-cap-ended and final scenes.

   D5 revision note: the original design crossfaded scenes — the previous
   scene extended into the next so the renderer could overlap the alpha ramps.
   Frame-by-frame verification of the reference edit showed the opposite:
   phrases replace each other with HARD CUTS, never two phrases on screen at
   once, with occasional ~0.1-0.2s fully-empty frames between scenes. Scenes
   now end ``SCENE_CLEAR_GAP_S`` before the next begins.

Karaoke learning baked in (D16): every display anchor keys off word START
times — a scene appears when its first word starts, and is replaced when the
next scene's first word starts. Whisper end timestamps drift on trailing
vowels/music; start times are the only trustworthy sync anchor. End times are
used solely to *measure* silence (pause-gap splits, hold-cap), never to place
text.

RHYTHM MODE (no ASR): editorial variants WITHOUT eligible speech pace an
authored multi-phrase quote across the video instead.
``synthesize_phrase_timings`` fabricates deterministic word timings from the
quote text alone — sentences get equal time slots across
``[RHYTHM_LEAD_IN_S, duration - RHYTHM_TAIL_S]``, words inside a sentence
spread contiguously across the first ``RHYTHM_SPEAK_FRAC`` of the slot
weighted by character length — and the synthesized words feed the SAME
``split_phrases`` pipeline above, so scene windows, clear gaps and fade rules
are shared with the transcript-synced path. ``rhythm_scenes`` is the one-call
convenience wrapper.

Everything here is deterministic: same words in, byte-identical scenes out.
``word_roles`` is emitted as ``None`` — the downstream emphasis agent fills it
later; this module never invents styling.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

import structlog

log = structlog.get_logger()

PAUSE_GAP_S = 0.35  # silence gap > this between consecutive words splits phrases
MAX_PHRASE_WORDS = 6  # a phrase never exceeds this; split mid-run when needed
MIN_SPEECH_WORDS = 4  # fewer total usable words → not eligible for the sequence
HOLD_CAP_S = 4.0  # a scene holds at most this long past its last word before fading
SCENE_CLEAR_GAP_S = 0.1  # all-clear gap between a scene's cut-end and the next scene
MIN_SCENE_GAP_S = 0.034  # degenerate-scene floor: keep at least one frame (~30fps) clear
FADE_OUT_S = 0.4  # fade-out duration when a scene ends by hold-cap or video end
COVERAGE_MIN_FRAC = 0.5  # speech must span at least this fraction of the timeline
LEAD_IN_CLAMP_S = 0.3  # scene 0 snaps to t=0 when speech starts within this window

# -- rhythm mode (synthesized timings, no ASR) -----------------------------------
RHYTHM_LEAD_IN_S = 0.3  # first sentence slot starts here, not at t=0
RHYTHM_TAIL_S = 0.8  # sentence slots end this far before the video ends
RHYTHM_SPEAK_FRAC = 0.62  # words occupy the first fraction of each slot; rest is air
RHYTHM_MIN_CHAR_WEIGHT = 2  # short tokens still get a readable minimum span

_TERMINAL_PUNCT = ".!?…"
# Closing wrappers that may trail the terminal punctuation ("word.")  etc.
_TRAILING_WRAPPERS = "\"'»”’)]}"
# Rhythm-mode sentence boundary: whitespace right after terminal punctuation.
# Whitespace is REQUIRED so a decimal/abbreviation ("3.5", "U.S.") stays in one
# sentence and a no-space typo ("hard.No") does not silently split mid-word.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text: str) -> list[str]:
    """Split text into rhythm-mode sentences on whitespace-after-terminal-punct.

    This is the SINGLE source of truth for sentence boundaries: both
    `synthesize_phrase_timings` (the engine) and the sequence-quote agent's
    structural validator import this, so the agent can never green-light a quote
    the engine would split differently (the regex-divergence corruption class).
    """
    return [s for s in _SENTENCE_SPLIT_RE.split(str(text).strip()) if s.strip()]


class _TimedWord(NamedTuple):
    text: str
    start: float
    end: float


def _field(word: Any, names: tuple[str, ...]) -> Any:
    """First non-None value among `names`, via dict key OR attribute access."""
    for name in names:
        if isinstance(word, dict):
            value = word.get(name)
        else:
            value = getattr(word, name, None)
        if value is not None:
            return value
    return None


def _normalize_words(words: list | None) -> list[_TimedWord]:
    """Coerce heterogeneous word records into timed tuples.

    Accepts transcribe.Word dataclasses (``start_s``/``end_s``) and persisted
    plain dicts (``start``/``end`` or ``start_s``/``end_s``). Words with
    missing/None timestamps or empty text are skipped (defensive).
    """
    if not words:
        return []
    normalized: list[_TimedWord] = []
    for word in words:
        text = _field(word, ("text", "word"))
        start = _field(word, ("start", "start_s"))
        end = _field(word, ("end", "end_s"))
        if text is None or start is None or end is None:
            continue
        if not str(text).strip():
            continue
        try:
            normalized.append(_TimedWord(text=text, start=float(start), end=float(end)))
        except (TypeError, ValueError):
            continue
    return normalized


def _ends_with_terminal_punct(text: str) -> bool:
    stripped = str(text).strip().rstrip(_TRAILING_WRAPPERS)
    return bool(stripped) and stripped[-1] in _TERMINAL_PUNCT


def speech_eligibility(words: list, *, video_duration_s: float) -> dict:
    """Gate (D8): is this transcript coherent enough for the typographic sequence?

    `words`: objects or dicts with text/start/end fields (both attribute and
    dict access accepted — transcribe.Word is a dataclass with ``start_s`` /
    ``end_s``; persisted transcripts are plain dicts).

    Returns ``{"eligible", "reason", "word_count", "coverage_frac"}``.
    Reasons, in check order: ``"too_few_words"`` (< MIN_SPEECH_WORDS usable
    words), ``"bad_duration"`` (video_duration_s <= 0), ``"low_coverage"``
    (speech span / duration < COVERAGE_MIN_FRAC), else ``"ok"``. Coverage is
    ``(last word end - first word start) / video_duration_s`` clamped to [0, 1].
    """
    normalized = _normalize_words(words)
    word_count = len(normalized)

    coverage_frac = 0.0
    if normalized and video_duration_s > 0:
        span_s = normalized[-1].end - normalized[0].start
        coverage_frac = round(max(0.0, min(1.0, span_s / video_duration_s)), 3)

    def _verdict(eligible: bool, reason: str) -> dict:
        if not eligible:
            log.debug(
                "phrase_sequence_ineligible",
                reason=reason,
                word_count=word_count,
                coverage_frac=coverage_frac,
            )
        return {
            "eligible": eligible,
            "reason": reason,
            "word_count": word_count,
            "coverage_frac": coverage_frac,
        }

    if word_count < MIN_SPEECH_WORDS:
        return _verdict(False, "too_few_words")
    if video_duration_s <= 0:
        return _verdict(False, "bad_duration")
    if coverage_frac < COVERAGE_MIN_FRAC:
        return _verdict(False, "low_coverage")
    return _verdict(True, "ok")


def split_phrases(words: list, *, video_duration_s: float) -> list[dict]:
    """Group words into ordered phrase scenes. Returns [] when not eligible
    (callers use ``speech_eligibility`` for the reason).

    Split boundaries, in priority order: terminal punctuation (. ! ? …) ends
    the phrase AFTER that word; a pause gap (next.start - cur.end >
    PAUSE_GAP_S) ends the phrase; a phrase reaching MAX_PHRASE_WORDS ends
    (cap-split).

    Each scene dict::

        {"words": [str, ...],      # the words, original text preserved
         "word_roles": None,       # placeholder; the emphasis agent fills this
         "speech_start_s": float,  # first word start
         "speech_end_s": float,    # last word end
         "start_s": float,         # display start = speech_start_s; scene 0
                                   #   clamps to 0.0 when speech_start_s <
                                   #   LEAD_IN_CLAMP_S (text on first frame)
         "end_s": float,           # display end (D5 rules below)
         "fade_out": bool}         # True ONLY when the scene ends by
                                   #   hold-cap or video end (soft fade in
                                   #   the renderer); False when replaced by
                                   #   the next scene (HARD CUT, no fade)

    Display-end rules (D5, revised — reference-verified hard cuts replaced
    the crossfade design): a scene normally ends BEFORE the next scene starts
    with an all-clear gap → ``end_s = next.start_s - SCENE_CLEAR_GAP_S``,
    ``fade_out=False``. Scenes NEVER overlap. Degenerate back-to-back
    phrases (where the full gap would push ``end_s`` at/under ``start_s``)
    shrink the gap to one frame instead: ``end_s = next.start_s -
    MIN_SCENE_GAP_S`` — and as a last resort against pathological sub-frame
    scene spacing (impossible with real Whisper output), ``end_s`` is forced
    strictly above ``start_s``; positive display duration always wins.

    HOLD-CAP: if the silence between ``speech_end_s`` and the next scene's
    start exceeds HOLD_CAP_S, the scene ends early: ``end_s = speech_end_s +
    HOLD_CAP_S + FADE_OUT_S`` (additionally clamped to the clear-gap ceiling
    so the no-overlap invariant is unconditional), ``fade_out=True``
    (text-free screen until the next scene). LAST scene: ``end_s =
    min(speech_end_s + HOLD_CAP_S, video_duration_s - 0.05) + FADE_OUT_S``
    clamped to ``video_duration_s``, ``fade_out=True``.

    All times rounded to 3 decimals.
    """
    if not speech_eligibility(words, video_duration_s=video_duration_s)["eligible"]:
        return []

    normalized = _normalize_words(words)
    groups = _group_words(normalized)

    # Display starts anchor on word START times (D16) — never end-based.
    display_starts = [group[0].start for group in groups]
    if display_starts and display_starts[0] < LEAD_IN_CLAMP_S:
        display_starts[0] = 0.0

    scenes: list[dict] = []
    for i, group in enumerate(groups):
        speech_start_s = group[0].start
        speech_end_s = group[-1].end
        if i + 1 < len(groups):
            next_start_s = display_starts[i + 1]
            # Clean-cut ceiling: clear the screen SCENE_CLEAR_GAP_S before the
            # next scene. Degenerate back-to-back phrases fall back to a
            # one-frame gap; a strictly positive display window always wins
            # over the gap (sub-frame scene spacing is pathological input).
            cut_end_s = next_start_s - SCENE_CLEAR_GAP_S
            if cut_end_s <= display_starts[i]:
                cut_end_s = next_start_s - MIN_SCENE_GAP_S
            if cut_end_s <= display_starts[i]:
                cut_end_s = display_starts[i] + 0.001  # last resort: end_s > start_s
            if next_start_s - speech_end_s > HOLD_CAP_S:
                # Hold-cap clamped to the cut ceiling: a gap barely over
                # HOLD_CAP_S must still never overlap the next scene.
                end_s = min(speech_end_s + HOLD_CAP_S + FADE_OUT_S, cut_end_s)
                fade_out = True
            else:
                end_s = cut_end_s
                fade_out = False
        else:
            hold_end_s = min(speech_end_s + HOLD_CAP_S, video_duration_s - 0.05)
            end_s = min(hold_end_s + FADE_OUT_S, video_duration_s)
            fade_out = True
        scenes.append(
            {
                "words": [w.text for w in group],
                "word_roles": None,
                "speech_start_s": round(speech_start_s, 3),
                "speech_end_s": round(speech_end_s, 3),
                "start_s": round(display_starts[i], 3),
                "end_s": round(end_s, 3),
                "fade_out": fade_out,
            }
        )

    log.info(
        "phrase_sequence_built",
        scene_count=len(scenes),
        word_count=len(normalized),
        video_duration_s=video_duration_s,
        window=scenes_total_window(scenes),
    )
    return scenes


def _group_words(normalized: list[_TimedWord]) -> list[list[_TimedWord]]:
    """Split the word stream into phrase groups (punctuation > pause > cap)."""
    groups: list[list[_TimedWord]] = []
    current: list[_TimedWord] = []
    for i, word in enumerate(normalized):
        current.append(word)
        nxt = normalized[i + 1] if i + 1 < len(normalized) else None
        boundary = (
            nxt is None
            or _ends_with_terminal_punct(word.text)
            or (nxt.start - word.end) > PAUSE_GAP_S
            or len(current) >= MAX_PHRASE_WORDS
        )
        if boundary:
            groups.append(current)
            current = []
    return groups


def synthesize_phrase_timings(text: str, *, video_duration_s: float) -> list[dict]:
    """Deterministic word-timing synthesis for rhythm mode (no ASR).

    Paces an authored multi-phrase quote across the video so the existing
    transcript-synced machinery (``split_phrases``) can consume it unchanged:

    1. Split ``text`` into sentences on whitespace following terminal
       punctuation (``. ! ? …``); empty sentences are dropped.
    2. The usable window is ``[RHYTHM_LEAD_IN_S, video_duration_s -
       RHYTHM_TAIL_S]``; each sentence gets an EQUAL time slot inside it.
    3. Words inside a sentence spread contiguously across the first
       ``RHYTHM_SPEAK_FRAC`` of the slot, each weighted by
       ``max(RHYTHM_MIN_CHAR_WEIGHT, len(token))`` — longer words hold longer.
       The remaining slot fraction is silent air, so consecutive sentences
       land in separate phrases (punctuation boundary AND a pause gap).

    Returns ``[{"word", "start_s", "end_s"}, ...]`` with times rounded to 3
    decimals — exactly the shape ``split_phrases`` consumes.

    Degenerate guards (return ``[]``): empty/whitespace ``text``;
    ``video_duration_s <= RHYTHM_LEAD_IN_S + RHYTHM_TAIL_S + 0.5`` (too short
    for a sequence). A single sentence is fine: it gets the whole window.
    """
    if not text or not str(text).strip():
        return []
    if video_duration_s <= RHYTHM_LEAD_IN_S + RHYTHM_TAIL_S + 0.5:
        log.debug(
            "rhythm_timings_skipped",
            reason="too_short",
            video_duration_s=video_duration_s,
        )
        return []

    sentences = split_sentences(text)
    if not sentences:
        return []

    window_start = RHYTHM_LEAD_IN_S
    window_end = video_duration_s - RHYTHM_TAIL_S
    slot_s = (window_end - window_start) / len(sentences)
    speak_s = slot_s * RHYTHM_SPEAK_FRAC

    timings: list[dict] = []
    for i, sentence in enumerate(sentences):
        tokens = sentence.split()
        weights = [max(RHYTHM_MIN_CHAR_WEIGHT, len(token)) for token in tokens]
        total_weight = sum(weights)
        cursor = window_start + i * slot_s
        for token, weight in zip(tokens, weights):
            duration_s = speak_s * weight / total_weight
            timings.append(
                {
                    "word": token,
                    "start_s": round(cursor, 3),
                    "end_s": round(cursor + duration_s, 3),
                }
            )
            cursor += duration_s

    log.debug(
        "rhythm_timings_synthesized",
        sentence_count=len(sentences),
        word_count=len(timings),
        video_duration_s=video_duration_s,
    )
    return timings


def rhythm_scenes(text: str, *, video_duration_s: float) -> list[dict]:
    """Rhythm-mode convenience: authored quote → phrase scenes in one call.

    ``split_phrases(synthesize_phrase_timings(text, ...), ...)`` — returns
    ``[]`` whenever synthesis is empty (degenerate text/duration) or the
    synthesized words fail the eligibility gate.
    """
    timings = synthesize_phrase_timings(text, video_duration_s=video_duration_s)
    if not timings:
        return []
    return split_phrases(timings, video_duration_s=video_duration_s)


def scenes_total_window(scenes: list[dict]) -> tuple[float, float]:
    """(first start_s, last end_s) of the sequence; (0.0, 0.0) when empty."""
    if not scenes:
        return (0.0, 0.0)
    return (scenes[0]["start_s"], scenes[-1]["end_s"])
