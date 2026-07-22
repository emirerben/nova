"""Readable, word-timed caption grammar for Smart talking-head edits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.smart_edit.presets import CaptionPolicy
from app.smart_edit.schemas import SemanticRole, SmartWord

_TOKEN_RE = re.compile(r"\S+")
_STRONG_END_RE = re.compile(r"[.!?…][\"')\]]*$")
# A standalone emphasis cue ("Messi" alone) shorter than this is held on screen
# up to this floor so a single word does not strobe past unreadably.
_STANDALONE_MIN_HOLD_S = 0.5

# Capitalized tokens that are NOT names — sentence-initial function words, etc.
# The deterministic emphasis floor also REQUIRES a scene-matcher entity anchor, so
# this stop-set only guards the OTHER words of a candidate name cue; it does not
# need to be exhaustive.
_NAME_STOP = frozenset(
    {
        "I",
        "The",
        "A",
        "An",
        "And",
        "But",
        "So",
        "Or",
        "He",
        "She",
        "It",
        "They",
        "We",
        "You",
        "This",
        "That",
        "My",
        "Your",
        "His",
        "Her",
        "Their",
        "Here",
        "There",
        "Now",
        "Then",
        "Maybe",
        "Number",
        "No",
        "Yes",
        "Which",
        "What",
        "When",
        "Where",
        "Who",
        "Why",
        "How",
        "Because",
        "Well",
        "Okay",
    }
)


def _is_name_token(text: str) -> bool:
    """A title-case, alphabetic token that is not a common capitalized function word."""

    token = text.strip().strip(".,!?;:\"')(")
    return bool(token) and token[0].isupper() and token.isalpha() and token not in _NAME_STOP


# A single word from this set must never render as its own caption — it is a bare
# list marker or function word (the "number" left alone when the following name
# isolates). Folded to lowercase before the check.
# NOTE: this list-marker/function-word vocabulary is mirrored (case-adjusted) in
# two sibling constants — `_NAME_STOP` above (title-case, to reject "Number" as a
# name) and `smart_edit.compiler._KEYWORD_STOP` (section-heading keyword picker).
# When adding or removing a marker here, update those together to avoid drift.
_LONE_MARKER_TOKENS = frozenset({"number", "no", "the", "a", "an", "and", "of", "to", "is", "are"})


def _is_lone_marker_token(text: str) -> bool:
    return text.strip().strip(".,!?;:\"')(").casefold() in _LONE_MARKER_TOKENS


def _is_lone_name_cue(chunk: list[SmartWord], anchors: set[str]) -> bool:
    """A short cue that is purely a scene-matcher-confirmed named entity.

    Requires 1-3 words, at least one word confirmed as an entity anchor (the
    scene matcher matched a visual to it), and every word to be either an anchor
    or a title-case name token — so a sentence fragment like "he is" or a bare
    marker like "number" is never promoted, only the name itself.
    """

    if not (1 <= len(chunk) <= 3):
        return False
    if not any(word.word_id in anchors for word in chunk):
        return False
    # Every word must read as a name (title-case, non-function) — so a
    # mis-anchored lowercase function word ("he") is never promoted.
    return all(_is_name_token(word.display_text) for word in chunk)


@dataclass(frozen=True, slots=True)
class _TimedToken:
    text: str
    start_s: float
    end_s: float
    timing_quality: str


def _tokens_for_cue(cue: dict[str, Any]) -> list[_TimedToken]:
    display_tokens = _TOKEN_RE.findall(str(cue.get("text") or "").strip())
    if not display_tokens:
        return []
    raw_words = cue.get("words")
    if isinstance(raw_words, list) and len(raw_words) == len(display_tokens):
        aligned: list[_TimedToken] = []
        try:
            for display, raw in zip(display_tokens, raw_words):
                if not isinstance(raw, dict):
                    raise ValueError
                start = max(0.0, float(raw.get("start_s", 0.0)))
                end = max(start + 0.01, float(raw.get("end_s", start + 0.01)))
                aligned.append(_TimedToken(display, start, end, "aligned"))
            return aligned
        except (TypeError, ValueError):
            pass

    start = max(0.0, float(cue.get("start_s", 0.0) or 0.0))
    end = max(start + 0.01, float(cue.get("end_s", start + 0.01) or start + 0.01))
    step = (end - start) / len(display_tokens)
    return [
        _TimedToken(token, start + index * step, start + (index + 1) * step, "segment_estimate")
        for index, token in enumerate(display_tokens)
    ]


def _should_close(
    current: list[_TimedToken],
    next_token: _TimedToken | None,
    policy: CaptionPolicy,
) -> bool:
    if len(current) >= policy.max_words:
        return True
    if len(" ".join(token.text for token in current)) >= policy.max_chars:
        return True
    if len(current) < policy.min_words:
        return False
    if _STRONG_END_RE.search(current[-1].text):
        return True
    if next_token is not None and next_token.start_s - current[-1].end_s >= 0.34:
        return True
    return False


def build_smart_caption_cues(
    cues: list[dict[str, Any]],
    policy: CaptionPolicy,
) -> list[dict[str, Any]]:
    """Re-chunk corrected cues into short readable phrases.

    The legacy captioner remains untouched. Smart Captions flattens the final
    corrected word timeline, then closes phrases on punctuation, real pauses,
    the preset word limit, or the preset character limit. Every output cue
    carries its word timings so highlighting, authored-text claims, and SFX
    anchors all share one clock.
    """

    tokens = [token for cue in cues for token in _tokens_for_cue(cue)]
    if not tokens:
        return []

    chunks: list[list[_TimedToken]] = []
    current: list[_TimedToken] = []
    for index, token in enumerate(tokens):
        current.append(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if _should_close(current, following, policy):
            chunks.append(current)
            current = []
    if current:
        if (
            chunks
            and len(current) < policy.min_words
            and len(chunks[-1]) + len(current) <= policy.max_words
        ):
            chunks[-1].extend(current)
        else:
            chunks.append(current)

    result: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk:
            continue
        result.append(
            {
                "text": " ".join(token.text for token in chunk),
                "start_s": round(chunk[0].start_s, 3),
                "end_s": round(chunk[-1].end_s, 3),
                "words": [
                    {
                        "text": token.text,
                        "start_s": round(token.start_s, 3),
                        "end_s": round(token.end_s, 3),
                        "timing_quality": token.timing_quality,
                    }
                    for token in chunk
                ],
            }
        )
    return result


def build_semantic_caption_cues(
    words: list[SmartWord],
    policy: CaptionPolicy,
    *,
    role_by_word_id: dict[str, SemanticRole],
    boundary_after_word_ids: set[str] | None = None,
    standalone_spans: list[list[str]] | None = None,
    keep_together_spans: list[list[str]] | None = None,
    entity_anchor_word_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build v2 presentation cues without allowing them to own semantics.

    ``SmartWord`` is already the canonical timed timeline when this function is
    called.  It may only group those words for readability, and it must close a
    cue before a role change or an authored-title boundary.  Word IDs stay on
    each cue so the compiler can style and claim exact spans later.

    Emphasis spans (plan 011, Feature A) refine word grouping without owning
    semantics:

    * ``standalone_spans`` — each span renders as its OWN cue (the founder's
      "number one → Messi shows Messi alone"): the chunker closes before the
      span's first word and after its last word.
    * ``keep_together_spans`` — the chunker may never close mid-span; when the
      whole span cannot join the current cue without breaching the word/char
      cap it closes EARLY, before the span, rather than splitting it.

    A semantic close (role change or authored-title boundary) ALWAYS wins over
    both — presentation may never override meaning. When ``standalone_spans`` and
    ``keep_together_spans`` are both empty the output is byte-identical to the
    pre-feature behavior (the ``SMART_CAPTION_EMPHASIS_CUES_ENABLED`` kill
    switch).

    Reliability floors (plan 012, P0):

    * ``entity_anchor_word_ids`` — word IDs the scene matcher confirmed as salient
      named entities (it matched a visual to them). A cue that ended up isolated
      and is purely such a name is promoted to ``smart_emphasis`` even when the
      LLM forgot to tag a standalone span, so a real name never renders un-styled.
    * A sub-``min_words`` marker chunk stranded right before a standalone cue (the
      classic "number" left alone when the following name isolates, worsened by an
      ASR split of "number one") is merged back into its previous neighbor instead
      of rendering as a lone one-word caption.

    Both floors are inert unless an emphasis span exists (merge-back) or an entity
    anchor is supplied (floor), so the spans-empty output stays byte-identical.
    """

    if not words:
        return []
    forced_breaks = boundary_after_word_ids or set()
    index_by_id = {word.word_id: idx for idx, word in enumerate(words)}

    # Resolve validated spans to index ranges. Spans reference existing,
    # contiguous, non-overlapping words (guaranteed upstream); any stale id is
    # ignored so a late transcript edit can never raise here.
    span_ranges: list[tuple[int, int, str]] = []
    span_start_at: dict[int, tuple[int, int, str]] = {}
    protected_after: set[int] = set()  # closing AFTER this index would split a span
    standalone_end: set[int] = set()

    def _register(groups: list[list[str]] | None, kind: str) -> None:
        for group in groups or []:
            idxs = [index_by_id[word_id] for word_id in group if word_id in index_by_id]
            if not idxs:
                continue
            start, end = min(idxs), max(idxs)
            span_ranges.append((start, end, kind))
            span_start_at[start] = (start, end, kind)
            protected_after.update(range(start, end))
            if kind == "standalone":
                standalone_end.add(end)

    _register(standalone_spans, "standalone")
    _register(keep_together_spans, "keep_together")
    standalone_ranges = {(s, e) for (s, e, kind) in span_ranges if kind == "standalone"}
    # Multi-word spans (either kind) must also stay on one line — surfaced to the
    # layout pass as cue-relative keep-together pairs.
    line_pairs = [(s, e) for (s, e, _kind) in span_ranges if e > s]

    timed = [
        _TimedToken(
            text=word.display_text,
            start_s=word.start_ms / 1000,
            end_s=word.end_ms / 1000,
            timing_quality=word.timing_quality,
        )
        for word in words
    ]
    chunks: list[tuple[list[SmartWord], SemanticRole]] = []
    current_words: list[SmartWord] = []
    current_tokens: list[_TimedToken] = []
    current_role: SemanticRole = role_by_word_id.get(words[0].word_id, "example")
    for index, (word, token) in enumerate(zip(words, timed)):
        role = role_by_word_id.get(word.word_id, "example")
        close_before = bool(current_words) and role != current_role
        span_starting_here = span_start_at.get(index)
        if current_words and span_starting_here is not None and not close_before:
            _span_start, span_end, span_kind = span_starting_here
            if span_kind == "standalone":
                close_before = True
            else:
                # keep_together: break early only if the whole span cannot join
                # the current cue without breaching a cap. Never split it.
                projected_words = len(current_words) + (span_end - index + 1)
                projected_text = " ".join(
                    [tok.text for tok in current_tokens]
                    + [words[i].display_text for i in range(index, span_end + 1)]
                )
                if projected_words > policy.max_words or len(projected_text) >= policy.max_chars:
                    close_before = True
        if close_before:
            chunks.append((current_words, current_role))
            current_words = []
            current_tokens = []
            current_role = role
        current_words.append(word)
        current_tokens.append(token)
        following = timed[index + 1] if index + 1 < len(timed) else None
        following_role = (
            role_by_word_id.get(words[index + 1].word_id, "example")
            if index + 1 < len(words)
            else None
        )
        # A semantic close (boundary or role change) always wins; only the
        # cap/pause close is suppressed to keep a span whole.
        semantic_close = word.word_id in forced_breaks or following_role != current_role
        cap_pause_close = _should_close(current_tokens, following, policy)
        if index in protected_after and not semantic_close:
            cap_pause_close = False
        should_close = semantic_close or cap_pause_close or index in standalone_end
        if should_close:
            chunks.append((current_words, current_role))
            current_words = []
            current_tokens = []
            if following_role is not None:
                current_role = following_role
    if current_words:
        chunks.append((current_words, current_role))

    # P0-2: fold a stranded marker chunk back into its previous neighbor so it
    # never renders as a lone one-word caption. Two triggers: a sub-min-words
    # chunk directly before a standalone-SPAN cue, OR any single bare marker /
    # function word ("number" left when the following name isolates — including a
    # name promoted by the entity FLOOR below, which is not a span). Only runs
    # when the emphasis feature is active, so the spans-empty output stays
    # byte-identical. Never merges into or across a standalone/boundary.
    emphasis_active = (
        bool(standalone_ranges) or bool(line_pairs) or entity_anchor_word_ids is not None
    )
    if emphasis_active:
        chunk_range = [(index_by_id[c[0].word_id], index_by_id[c[-1].word_id]) for c, _r in chunks]
        # An entity-FLOOR name ("Messi" promoted below without an LLM standalone
        # span) must get the SAME merge-back protection as a real standalone span:
        # never fold it away, and never glue a marker into or lead one into it —
        # otherwise a lone "number" before a floor name would strip that name's
        # emphasis, defeating the floor in the multi-item-list case it targets.
        floor_name_ranges: set[tuple[int, int]] = set()
        if entity_anchor_word_ids:
            floor_name_ranges = {
                chunk_range[i]
                for i, (c, _r) in enumerate(chunks)
                if _is_lone_name_cue(c, entity_anchor_word_ids)
            }
        protected_ranges = standalone_ranges | floor_name_ranges
        merged: list[tuple[list[SmartWord], SemanticRole]] = []
        pending_prefix: list[SmartWord] = []  # a lone marker deferred to lead into the next cue
        for i, (chunk_words, chunk_role) in enumerate(chunks):
            if pending_prefix:
                chunk_words = pending_prefix + chunk_words
                pending_prefix = []
            is_span_stranded = (
                len(chunk_words) < policy.min_words
                and chunk_range[i] not in protected_ranges
                and i + 1 < len(chunks)
                and chunk_range[i + 1] in standalone_ranges
            )
            is_lone_marker = (
                len(chunk_words) == 1
                and chunk_range[i] not in protected_ranges
                and _is_lone_marker_token(chunk_words[0].display_text)
            )
            if is_span_stranded or is_lone_marker:
                # Prefer folding into the previous cue; never into/across a
                # standalone, a floor name, or an authored boundary.
                if merged:
                    prev_words, prev_role = merged[-1]
                    prev_range = (
                        index_by_id[prev_words[0].word_id],
                        index_by_id[prev_words[-1].word_id],
                    )
                    if (
                        prev_range not in protected_ranges
                        and prev_words[-1].word_id not in forced_breaks
                    ):
                        merged[-1] = (prev_words + chunk_words, prev_role)
                        continue
                # Backward blocked (e.g. "and" wedged between two isolated names):
                # lead the marker into the NEXT cue instead, unless the next cue is
                # itself a standalone / floor name or an authored boundary intervenes.
                if (
                    is_lone_marker
                    and i + 1 < len(chunks)
                    and chunk_range[i + 1] not in protected_ranges
                    and chunk_words[-1].word_id not in forced_breaks
                ):
                    pending_prefix = chunk_words
                    continue
            merged.append((chunk_words, chunk_role))
        if pending_prefix:  # never drop a deferred marker
            merged.append((pending_prefix, current_role))
        chunks = merged

    result: list[dict[str, Any]] = []
    for chunk, role in chunks:
        if not chunk:
            continue
        first_global = index_by_id[chunk[0].word_id]
        last_global = index_by_id[chunk[-1].word_id]
        cue: dict[str, Any] = {
            "text": " ".join(word.display_text for word in chunk),
            "start_s": round(chunk[0].start_ms / 1000, 3),
            "end_s": round(chunk[-1].end_ms / 1000, 3),
            "words": [
                {
                    "text": word.display_text,
                    "start_s": round(word.start_ms / 1000, 3),
                    "end_s": round(word.end_ms / 1000, 3),
                    "timing_quality": word.timing_quality,
                }
                for word in chunk
            ],
            "smart_word_ids": [word.word_id for word in chunk],
            "smart_role": role,
        }
        if (first_global, last_global) in standalone_ranges:
            cue["smart_emphasis"] = True
        elif entity_anchor_word_ids and _is_lone_name_cue(chunk, entity_anchor_word_ids):
            # P0-1 floor: an isolated cue that is purely a scene-matcher-confirmed
            # name gets emphasized even when the LLM missed the standalone span.
            cue["smart_emphasis"] = True
        kept = [
            [start - first_global, end - first_global]
            for (start, end) in line_pairs
            if first_global <= start and end <= last_global
        ]
        if kept:
            cue["smart_keep_together"] = kept
        result.append(cue)

    _apply_standalone_min_hold(result)
    return result


def _apply_standalone_min_hold(cues: list[dict[str, Any]]) -> None:
    """Keep a standalone one-word cue on screen long enough to read.

    Floor-only: extend a standalone cue's DISPLAY end toward
    ``start + _STANDALONE_MIN_HOLD_S`` but
    never past the next cue's start (no overlap) and never earlier than it
    already ends. Continuous speech with no gap gets no extension — mirrors the
    shipped word-cue floor, which never invents time by overrunning the next
    word.

    Only the cue's on-screen ``end_s`` is extended; the per-word ``end_s`` is
    left at the real spoken end so word-level consumers (speech map, SFX-at-word
    sync, word highlighting) stay truthful. A caption lingering past its last
    word is normal and safe (ASS clips at the video's end).
    """

    for i, cue in enumerate(cues):
        if not cue.get("smart_emphasis"):
            continue
        start = float(cue["start_s"])
        end = float(cue["end_s"])
        if end - start >= _STANDALONE_MIN_HOLD_S:
            continue
        target = start + _STANDALONE_MIN_HOLD_S
        if i + 1 < len(cues):
            target = min(target, float(cues[i + 1]["start_s"]))
        if target > end:
            cue["end_s"] = round(target, 3)
