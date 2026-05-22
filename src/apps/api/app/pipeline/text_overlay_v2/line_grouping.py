"""Group atomized phrases into progressive-reveal line groups.

Stage E+: after `text_alignment` corrects OCR text against the Whisper
transcript, this module groups consecutive atomized phrases (one word each)
that should render as a single progressive-reveal line. Stage G consumes the
groups and emits cumulative-text overlays via `text_reveal.build_cumulative_stages`.

Grouping rules (line source = Whisper transcript only):
- Each phrase is matched to a transcript word (casefold, time-proximate).
- Phrases that don't match any transcript word are SKIPPED — they do NOT
  close the running group. OCR noise (single-char artifacts, stray
  punctuation) and legitimate words the transcript happened to miss should
  not fragment a cumulative reveal mid-phrase. Groups close only on real
  terminators (sentence punctuation in the transcript between matched
  words, silence gap above `silence_gap_s`, or `max_words_per_line` cap).
  Regression: prod job 09f56ee3 (2026-05-22) rendered "The work to get"
  as a cumulative reveal but the trailing "there" was emitted as a
  standalone overlay because an unmatched OCR phrase between them closed
  the group; later "allow / allow anyone" was the only fragment of
  "don't allow anyone to diminish your hard work" that grouped at all.
- Phrases that match different transcript words become group-mates only if
  the transcript span between them contains NO sentence-terminating
  punctuation (`.`, `?`, `!`) AND the silence gap between their transcript
  words is below `silence_gap_s` AND the running group hasn't exceeded
  `max_words_per_line`.
- Only groups of `min_group_size` (default 2+) are emitted. A singleton would
  just be a left-anchored static word, no real progressive reveal — better
  to pass it through Stage G's unchanged path.

The output is a list of `LineGroup`s. Phrases NOT covered by any group are
the caller's responsibility (Stage G emits them via the existing per-phrase
overlay path).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents._schemas.text_alignment import TranscriptWord
from app.agents._schemas.text_overlay_pipeline import Phrase

DEFAULT_SILENCE_GAP_S = 0.7
DEFAULT_MAX_WORDS_PER_LINE = 8
DEFAULT_MIN_GROUP_SIZE = 2

# Time tolerance for matching an atomized phrase to a transcript word. The
# phrase's `start_t_s` is the OCR first-seen frame; transcript word start_s is
# vocal onset. These can drift by a second or more when text appears slightly
# before or after the vocal. Tighter than this and atomized matches fall
# through to "ungrouped"; looser and a word can match the wrong vocal far away.
MATCH_TIME_TOLERANCE_S = 3.0

# Sentence-terminating punctuation. Apostrophes and commas do NOT split lines
# — "it's the way you walk" should reveal as one line.
_SENTENCE_TERMINATORS = frozenset(".?!")


@dataclass(frozen=True, slots=True)
class LineGroup:
    """A progressive-reveal line: N consecutive atomized phrases that share
    one visible line on screen, build up word-by-word, then clear together.

    `phrase_indices` are indices into the post-alignment `phrases` list (the
    list Stage G iterates). The order is the reveal order (matches transcript
    time).

    `line_end_s` is when the cumulative line clears. Set to the natural end
    of the last word's transcript span; Stage G's cumulative emitter adds
    the `LAST_WORD_DWELL_S` on top.

    `line_anchor_x_frac` is the LEFT edge of the line on screen, taken from
    the first phrase's OCR bbox left edge (`aabb[0]`). Stage G writes this
    into the cumulative overlay's `bbox.x_norm` with `text_anchor="left"` so
    the line grows rightward without re-centering.

    `transcript_word_indices` is parallel to `phrase_indices` and records
    which transcript word each phrase matched. Useful for Stage G to read
    accurate word.start_s timings (transcript times are more reliable than
    OCR first-seen frames).
    """

    phrase_indices: list[int]
    line_end_s: float
    line_anchor_x_frac: float
    # Y center of the line on screen, taken from the first phrase's aabb.
    # Stage G writes this into `bbox.y_norm` so `_bbox_to_named_position`
    # correctly buckets the cumulative reveal to top/center/bottom. Without
    # it, every cumulative reveal would render at canvas center regardless
    # of where the OCR detected the line.
    line_anchor_y_frac: float
    # Approximate height of the line bbox, also from the first phrase.
    # Used as `bbox.h_norm` so the TextBBox is meaningful (not just a
    # placeholder) and downstream consumers see a faithful bbox.
    line_height_frac: float
    transcript_word_indices: list[int]
    # Per-word transcript start_s, parallel to phrase_indices. Stage G uses
    # these (more accurate than OCR first-seen frame) to time each cumulative
    # reveal stage. Pulled from the matched transcript word at group-build
    # time so Stage G doesn't need the transcript_words list.
    word_start_s_list: list[float]


def build_line_groups(
    phrases: list[Phrase],
    transcript_words: list[TranscriptWord] | list[dict],
    *,
    silence_gap_s: float = DEFAULT_SILENCE_GAP_S,
    max_words_per_line: int = DEFAULT_MAX_WORDS_PER_LINE,
    min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
) -> list[LineGroup]:
    """Return progressive-reveal line groups for the given atomized phrases.

    A phrase is "atomized" when its `lines` field has exactly one entry and
    that entry is a single word. Multi-line phrases (build-up captions) and
    multi-word phrases are NOT grouped — they pass through to Stage G as is.

    `transcript_words` may be Pydantic `TranscriptWord` instances or raw
    dicts with `text`, `start_s`, `end_s` keys. The function normalizes both.

    Returns empty list when:
      - `phrases` is empty, OR
      - `transcript_words` is empty (no anchor signal — every phrase ungrouped), OR
      - no atomized phrase finds a transcript match.
    """
    if not phrases or not transcript_words:
        return []

    # Normalize transcript words to a uniform dict shape. Use isinstance over
    # `getattr(...) or w[...]` because 0.0 timestamps would short-circuit the
    # `or` to the dict-access fallback and break on Pydantic models.
    norm_words = []
    for w in transcript_words:
        if isinstance(w, dict):
            text = w["text"]
            start_s = w["start_s"]
            end_s = w["end_s"]
        else:
            text = w.text
            start_s = w.start_s
            end_s = w.end_s
        norm_words.append(
            {
                "text": _normalize_word(text),
                "start_s": float(start_s),
                "end_s": float(end_s),
            }
        )

    # Match each atomized phrase to a transcript word index, walking the
    # transcript forward so each word is used at most once. Non-atomized
    # phrases and phrases without a match remain unmatched.
    matches: list[int | None] = []
    transcript_cursor = 0  # earliest transcript word still available
    for phrase in phrases:
        if not _is_atomized(phrase):
            matches.append(None)
            continue
        phrase_word = _normalize_word(phrase.lines[0])
        if not phrase_word:
            matches.append(None)
            continue
        match_idx = _find_match(
            phrase_word,
            phrase.start_t_s,
            norm_words,
            transcript_cursor,
        )
        matches.append(match_idx)
        if match_idx is not None:
            transcript_cursor = match_idx + 1

    # Walk phrases in order. Open a group when a matched phrase starts a new
    # line; close it when the next matched phrase crosses a boundary
    # (sentence terminator, silence gap, max-words cap, or unmatched phrase).
    groups: list[LineGroup] = []
    current: list[int] = []  # phrase indices in the open group
    current_tw: list[int] = []  # transcript word indices in the open group

    def _close():
        if len(current) >= min_group_size:
            first_phrase = phrases[current[0]]
            last_tw_idx = current_tw[-1]
            line_end_s = norm_words[last_tw_idx]["end_s"]
            # aabb is (x_min, y_min, x_max, y_max). Left edge for x anchor,
            # y center for vertical position, height for bbox h_norm.
            x_anchor = float(first_phrase.aabb[0])
            y_center = float((first_phrase.aabb[1] + first_phrase.aabb[3]) / 2.0)
            h_extent = float(first_phrase.aabb[3] - first_phrase.aabb[1])
            word_starts = [norm_words[tw_idx]["start_s"] for tw_idx in current_tw]
            groups.append(
                LineGroup(
                    phrase_indices=list(current),
                    line_end_s=line_end_s,
                    line_anchor_x_frac=x_anchor,
                    line_anchor_y_frac=y_center,
                    line_height_frac=max(h_extent, 0.01),  # min epsilon for TextBBox
                    transcript_word_indices=list(current_tw),
                    word_start_s_list=word_starts,
                )
            )
        current.clear()
        current_tw.clear()

    for phrase_idx, match_idx in enumerate(matches):
        if match_idx is None:
            # Unmatched phrase (OCR artifact, non-atomized, or a real word
            # the transcript missed). SKIP — do NOT close the running group.
            # The group's silence/terminator/max-words checks operate on the
            # next *matched* phrase against the *last* matched one, so
            # skipping unmatched preserves correct boundary detection while
            # keeping the cumulative reveal intact. Skipped phrases fall
            # through to Stage G's per-phrase fallback emit (where Stage D's
            # artifact filter has already dropped pure-noise tokens).
            continue

        if not current:
            current.append(phrase_idx)
            current_tw.append(match_idx)
            continue

        # Check boundary against the previous matched word.
        prev_tw_idx = current_tw[-1]
        prev_word = norm_words[prev_tw_idx]
        this_word = norm_words[match_idx]

        if _has_sentence_terminator(transcript_words, prev_tw_idx, match_idx):
            _close()
            current.append(phrase_idx)
            current_tw.append(match_idx)
            continue

        silence = this_word["start_s"] - prev_word["end_s"]
        if silence > silence_gap_s:
            _close()
            current.append(phrase_idx)
            current_tw.append(match_idx)
            continue

        if len(current) >= max_words_per_line:
            _close()
            current.append(phrase_idx)
            current_tw.append(match_idx)
            continue

        current.append(phrase_idx)
        current_tw.append(match_idx)

    _close()
    return groups


# ── Internals ─────────────────────────────────────────────────────────────────


def _is_atomized(phrase: Phrase) -> bool:
    """A phrase is atomized when it carries exactly one line containing a
    single whitespace-separated token. Stage D in `atomize_per_event` mode
    produces these; multi-line captions and multi-word lines are not."""
    if len(phrase.lines) != 1:
        return False
    text = phrase.lines[0].strip()
    if not text:
        return False
    return len(text.split()) == 1


def _normalize_word(text: str | None) -> str:
    """Casefold + strip surrounding punctuation for matching.

    Apostrophes and hyphens inside words are kept (`it's`, `co-op`).
    Trailing `.,?!:;` and surrounding quotes are stripped — Whisper often
    appends a comma to a word that OCR rendered without one.
    """
    if text is None:
        return ""
    s = text.strip().casefold()
    # Strip leading/trailing non-alphanumeric while preserving inner punctuation.
    while s and not s[0].isalnum():
        s = s[1:]
    while s and not s[-1].isalnum():
        s = s[:-1]
    return s


def _find_match(
    phrase_word: str,
    phrase_start_s: float,
    norm_words: list[dict],
    cursor: int,
) -> int | None:
    """Find the transcript word at or after `cursor` whose normalized text
    matches `phrase_word` and whose start_s is closest to `phrase_start_s`
    within `MATCH_TIME_TOLERANCE_S`. Returns None if no candidate qualifies.

    Walking forward from `cursor` enforces monotone consumption: a transcript
    word matched by phrase N is not available to phrase N+1, so repeated OCR
    detections of the same word don't all collapse onto the same transcript
    word.
    """
    best_idx: int | None = None
    best_delta = float("inf")
    for i in range(cursor, len(norm_words)):
        if norm_words[i]["text"] != phrase_word:
            continue
        delta = abs(norm_words[i]["start_s"] - phrase_start_s)
        if delta > MATCH_TIME_TOLERANCE_S:
            # Walking forward in time: once we're far enough past the phrase
            # to exceed tolerance, no later word can be closer. Break early.
            if norm_words[i]["start_s"] - phrase_start_s > MATCH_TIME_TOLERANCE_S:
                break
            continue
        if delta < best_delta:
            best_idx = i
            best_delta = delta
    return best_idx


def _has_sentence_terminator(
    transcript_words: list[TranscriptWord] | list[dict],
    prev_idx: int,
    curr_idx: int,
) -> bool:
    """True if any transcript word in [prev_idx, curr_idx) ends with `.?!`.

    Checks the RAW transcript text (not the normalized form) so trailing
    punctuation survives. Includes `prev_idx` itself — punctuation at the
    end of the previously matched word still terminates the sentence before
    the next word starts.
    """
    for i in range(prev_idx, curr_idx):
        w = transcript_words[i]
        if isinstance(w, dict):
            raw_text = w.get("text", "")
        else:
            raw_text = getattr(w, "text", "") or ""
        stripped = raw_text.rstrip(" \t\"')]}")
        if stripped and stripped[-1] in _SENTENCE_TERMINATORS:
            return True
    return False
