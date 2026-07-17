"""Generate ASS subtitle file from Whisper word timestamps.

ASS (Advanced SubStation Alpha) supports word-level highlight styling
which produces the "karaoke" caption effect common on TikTok/Reels.
"""

import re

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text
from app.pipeline.transcribe import Transcript, Word

# A word ends a sentence when its text ends in terminal punctuation, allowing a
# trailing closing quote/bracket ("...better!" or "...over."). Used to start a new
# caption cue at every sentence boundary.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'’”)\]]*$")
# A word ends a clause (comma/semicolon/colon) — the preferred break point when a
# single long sentence has to be split across caption blocks.
_CLAUSE_END_RE = re.compile(r"[,;:]+[\"'’”)\]]*$")
# A token carrying no word characters (a stray "." or "…") — attach it to the
# previous word instead of letting it float as its own token / cue.
_PURE_PUNCT_RE = re.compile(r"^[^\w]+$")
# Max words per caption block. A sentence longer than this splits into multiple
# blocks (preferably at a clause boundary) — but a block NEVER spans two sentences.
_MAX_CUE_WORDS = 14
# Word-by-word ("qbuilder") captions show ONE word at a time. A spoken word whose
# Whisper span is shorter than this floor would strobe on screen, so its on-screen
# end is extended up to this minimum — never past the next word's start.
_MIN_WORD_CUE_S = 0.18


def generate_ass(
    transcript: Transcript,
    start_s: float,
    end_s: float,
    output_path: str,
    *,
    style: str = "karaoke",
    font_name: str | None = None,
) -> None:
    """Write an ASS subtitle file for the clip window [start_s, end_s].

    Words are shifted so clip-start = time 0.

    ``style``:
      - ``"karaoke"`` (default) — word-by-word highlight (yellow on white),
        the TikTok/Reels effect used by the clip-transcription pipeline.
      - ``"plain"`` — static line captions (white, no per-word highlight),
        the "like subtitles" look the narrated archetype burns for the
        recorded voiceover.
    ``font_name``: ASS ``Fontname`` for the Default style. The name must
      resolve in the burn's ``fontsdir`` (libass matches by family name).
      ``None`` (default) keeps the legacy Arial header byte-for-byte so the
      existing caller's output is unchanged.
    """
    words = [w for w in transcript.words if start_s <= w.start_s < end_s]
    if not words:
        _write_empty_ass(output_path, font_name=font_name)
        return

    if style == "plain":
        lines = _build_plain_lines(words, offset_s=start_s)
    else:
        lines = _build_dialogue_lines(words, offset_s=start_s)

    _write_ass(lines, output_path, font_name=font_name)


def _coalesce_words(words: list[Word]) -> list[tuple[str, float, float]]:
    """Tokenize words into ``(text, start_s, end_s)`` tuples, dropping blanks and
    attaching a pure-punctuation token (a stray "." Whisper emits as its own word)
    to the previous token (no space, extending its end). The shared tokenization for
    BOTH sentence captions (:func:`_join_words`) and word-by-word (:func:`build_word_cues`)
    so the two never diverge on how punctuation is grouped."""
    out: list[tuple[str, float, float]] = []
    for w in words:
        t = w.text.strip()
        if not t:
            continue
        if _PURE_PUNCT_RE.match(t) and out:
            prev_text, prev_start, _prev_end = out[-1]
            out[-1] = (prev_text + t, prev_start, float(w.end_s))
        else:
            out.append((t, float(w.start_s), float(w.end_s)))
    return out


def _join_words(ws: list[Word]) -> str:
    """Join word texts with spaces, attaching a pure-punctuation token to the
    previous word (no space). Shares tokenization with :func:`build_word_cues`."""
    return " ".join(text for text, _start, _end in _coalesce_words(ws))


def _split_sentence_groups(sentence: list[Word]) -> list[list[Word]]:
    """Split ONE sentence's words into caption-sized groups (≤ _MAX_CUE_WORDS).

    A sentence at/under the cap is one group. A longer one breaks at the LAST clause
    boundary (comma/semicolon/colon) before the cap so the split reads naturally,
    falling back to a hard cap when there's no clause break in range. Every group is
    still from the SAME sentence — this only sub-divides, never merges across sentences.
    """
    n = len(sentence)
    if n <= _MAX_CUE_WORDS:
        return [sentence]
    groups: list[list[Word]] = []
    start = 0
    while start < n:
        hard_end = start + _MAX_CUE_WORDS
        if hard_end >= n:
            groups.append(sentence[start:n])
            break
        # Prefer a clause break, but not so early the block is tiny (≥ half the cap).
        soft_min = start + max(1, _MAX_CUE_WORDS // 2)
        split_at = hard_end
        for j in range(hard_end - 1, soft_min - 1, -1):
            if _CLAUSE_END_RE.search(sentence[j].text.strip()):
                split_at = j + 1
                break
        groups.append(sentence[start:split_at])
        start = split_at
    return groups


def build_plain_cues(
    words: list[Word], offset_s: float = 0.0, *, attach_words: bool = False
) -> list[dict]:
    """Group words into editable plain-caption cues — the source of truth for the
    on-video caption editor — splitting at SENTENCE boundaries.

    ``attach_words`` (word-by-word subtitled style): also attach each cue's real
    per-word timings as ``cue["words"] = [{text, start_s, end_s}]`` (offset-shifted),
    so the word-pop burn locks the highlight to the audio and a reburn can reuse them.
    Off (default) keeps cues as ``{text, start_s, end_s}`` — byte-identical to before.

    Each cue is ``{"text", "start_s", "end_s"}`` with RAW (un-sanitized) display text
    and times shifted by ``offset_s``. A new cue starts at every sentence end (terminal
    `.!?`), so two sentences are NEVER merged into one line. A sentence longer than
    ``_MAX_CUE_WORDS`` splits into several cues (preferably at a comma) — still never
    spanning two sentences. A transcript with no punctuation degrades to fixed-size
    chunks.

    Timing rules (kept locked to the audio):
      - Each cue starts at its first word's start.
      - A cue that is the LAST block of its sentence ends at its last word's end, so
        the caption clears during the inter-sentence pause (synced to the spoken audio).
      - A cue that is a NON-last block of a long sentence ends exactly where the next
        block begins — closing the inter-word micro-gap so the caption never flickers
        off mid-sentence during continuous speech.
    No word time is invented or shifted; only the grouping and the within-sentence
    block boundaries are set.
    """
    cues: list[dict] = []
    sentence: list[Word] = []

    def _emit() -> None:
        if not sentence:
            return
        groups = _split_sentence_groups(sentence)
        for k, g in enumerate(groups):
            if not g:
                continue
            text = _join_words(g)
            if not any(ch.isalnum() for ch in text):
                continue  # pure-punctuation block carries nothing readable
            cue_start = max(0.0, float(g[0].start_s) - offset_s)
            is_last_block = k == len(groups) - 1
            if not is_last_block and groups[k + 1]:
                # same-sentence continuation → hold until the next block (no flicker)
                cue_end = float(groups[k + 1][0].start_s) - offset_s
            else:
                # sentence's final block → end on the last spoken word (then the pause)
                cue_end = float(g[-1].end_s) - offset_s
            cue_end = max(cue_start + 0.01, cue_end)
            cue: dict = {"text": text, "start_s": round(cue_start, 3), "end_s": round(cue_end, 3)}
            if attach_words:
                # SAME tokenization as `text` (_coalesce_words): a stray punctuation-only
                # whisper token attaches to the previous word. Raw `g` words would carry
                # one MORE entry than text.split(), silently failing the aligned check
                # and demoting word-pop to synthesized timing.
                cue["words"] = [
                    {
                        "text": tok_text,
                        "start_s": round(max(0.0, tok_start - offset_s), 3),
                        "end_s": round(max(0.0, tok_end - offset_s), 3),
                    }
                    for tok_text, tok_start, tok_end in _coalesce_words(g)
                ]
            cues.append(cue)
        sentence.clear()

    for w in words:
        sentence.append(w)
        if _SENTENCE_END_RE.search(w.text.strip()):
            _emit()
    _emit()
    return cues


def build_word_cues(words: list[Word], offset_s: float = 0.0) -> list[dict]:
    """Group words into editable WORD-BY-WORD cues — the "qbuilder" caption style.

    Each cue is ``{"text", "start_s", "end_s"}`` holding exactly ONE spoken word,
    so captions advance a word at a time in sync with the voiceover (vs the sentence
    blocks of :func:`build_plain_cues`). Times are shifted by ``offset_s``. The cue
    list stays the editable source of truth — the on-video editor renders one row
    per word and a reburn re-burns the edited words.

    Rules (kept locked to the audio):
      - A pure-punctuation token (a stray ``.`` Whisper emits) attaches to the
        previous word's text instead of flashing as its own cue.
      - Each cue spans its word's ``[start, end]``. An ultra-short word is given a
        minimum on-screen floor (``_MIN_WORD_CUE_S``) so it doesn't strobe, but the
        floor never overruns the next word's start.
      - Times stay monotonic and non-overlapping; no word time is invented.
    """
    merged = _coalesce_words(words)
    cues: list[dict] = []
    n = len(merged)
    for i, (text, word_start, word_end) in enumerate(merged):
        if not any(ch.isalnum() for ch in text):
            continue  # carries nothing readable
        start = max(0.0, word_start - offset_s)
        end = word_end - offset_s
        next_start = (merged[i + 1][1] - offset_s) if i + 1 < n else None
        if end - start < _MIN_WORD_CUE_S:
            floored = start + _MIN_WORD_CUE_S
            end = min(floored, next_start) if next_start is not None else floored
        # Guard the docstring's monotonic/non-overlap contract against rare
        # out-of-order Whisper spans: never start before the previous cue ends.
        if cues:
            start = max(start, cues[-1]["end_s"])
        end = max(start + 0.01, end)
        cues.append({"text": text, "start_s": round(start, 3), "end_s": round(end, 3)})
    return cues


# Safe-zone MarginV for the subtitled single-clip style: ~20% up from the bottom
# (384/1920) so captions clear the TikTok/Reels/Shorts UI chrome (which owns the
# bottom ~15-20%) AND the speaker's chin, while staying lower-third. The narrated
# default stays 180 (9.4%) — its captions sit under a different composition. Both
# the first burn and the reburn MUST pass the same value or edited captions jump.
SUBTITLED_CAPTION_MARGIN_V = 384

# Kria lime accent (#84cc16) as an ASS inline colour (AABBGGRR, opaque). Used to pop
# the currently-spoken word in the word-by-word subtitled look — NOT the yellow-karaoke
# cliché (SecondaryColour sweep). See DESIGN.md §9 "one accent per surface".
_ACTIVE_WORD_ASS_COLOR = "&H0016CC84"

# Subtitled sentence pop-in: each caption fades in fast (120ms) and scales 94%→100%
# (140ms), then HARD-CUTS to the next cue — matching the editorial-sequence D5 rule
# (hard cuts, not crossfades). Applied per Dialogue line so the shared plain header
# stays byte-identical for narrated.
_SENTENCE_POP_TAGS = "{\\fad(120,0)\\fscx94\\fscy94\\t(0,140,\\fscx100\\fscy100)}"

# Smart Captions cue roles compile to a CLOSED set of ASS override blocks. The
# default/absent path emits no extra bytes, preserving every legacy/narrated ASS
# file. Colours use ASS BGR ordering: #84CC16 → &H16CC84&.
_SMART_CAPTION_TAGS: dict[str, str] = {
    "hook": "{\\fs82\\c&H16CC84&\\bord8\\shad2}",
    "context": "{\\fs76\\c&H16CC84&\\bord8\\shad2}",
    "list_item": "{\\fs82\\c&HFFFFFF&\\bord8\\shad2}",
    "example": "{\\fs70\\c&HF5F5F5&\\bord7\\shad2}",
    "payoff": "{\\fs82\\c&H16CC84&\\bord8\\shad2}",
    "cta": "{\\fs78\\c&H16CC84&\\bord8\\shad2}",
}

# Splits corrected text into display sentences at terminal punctuation. The
# negative lookbehind keeps Turkish ordinals/abbreviations together: "3. gün"
# must not split into a dangling "3." caption.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])(?<!\d\.)\s+")


def resplit_cues_into_sentences(cues: list[dict]) -> list[dict]:
    """Post-correction re-split: one SENTENCE per cue (the visual fix for stacked lines).

    Whisper often emits no punctuation, so ``build_plain_cues`` degrades to 14-word
    chunks; the LLM correction then adds real punctuation — leaving several sentences
    trapped in one giant cue that renders as a 4-line block. This re-splits each cue's
    corrected text at sentence boundaries and distributes the cue's time window across
    the pieces:

      - If the cue carries real per-word timings (``words``) that still spell its text,
        sentence boundaries land exactly on word times (audio-locked).
      - Otherwise time is split proportionally to each sentence's share of characters —
        good at cue granularity (a cue is a few seconds).

    A short tail fragment with no terminal punctuation (whisper split a sentence across
    cues, e.g. "… Ona" | "onar adet yapıyoruz.") is merged INTO the next cue so the
    sentence displays whole. Timing stays monotonic; total span is preserved.
    """
    # Flatten every cue into sentence pieces with their own [start, end].
    pieces: list[dict] = []
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        start = float(cue.get("start_s", 0.0))
        end = max(start + 0.01, float(cue.get("end_s", start)))
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        if len(sentences) <= 1:
            pieces.append(dict(cue))
            continue

        tokens = text.split()
        words = cue.get("words")
        aligned = (
            isinstance(words, list)
            and len(words) == len(tokens)
            and [str(w.get("text", "")).strip() for w in words] == tokens
        )
        # Cumulative token counts → boundary times (word-aligned or char-proportional).
        boundaries: list[tuple[float, float]] = []
        tok_i = 0
        char_pos = 0
        total_chars = max(1, len(text))
        cursor = start
        for k, s in enumerate(sentences):
            n_toks = len(s.split())
            if aligned:
                seg_start = float(words[tok_i]["start_s"]) if k > 0 else start
                last = words[min(tok_i + n_toks - 1, len(words) - 1)]
                seg_end = float(last["end_s"]) if k < len(sentences) - 1 else end
            else:
                char_pos += len(s) + (1 if k < len(sentences) - 1 else 0)
                seg_start = cursor
                seg_end = (
                    end
                    if k == len(sentences) - 1
                    else start + (end - start) * (char_pos / total_chars)
                )
            seg_end = max(seg_start + 0.01, seg_end)
            boundaries.append((seg_start, seg_end))
            cursor = seg_end
            tok_i += n_toks

        seg_tok_i = 0
        for s, (seg_start, seg_end) in zip(sentences, boundaries):
            piece: dict = {"text": s, "start_s": round(seg_start, 3), "end_s": round(seg_end, 3)}
            n_toks = len(s.split())
            if aligned:
                # Carry the REAL per-word timings onto the piece so the word-pop burn
                # stays audio-locked (an LLM-corrected cue already dropped its words →
                # aligned is False there and word-pop synthesizes instead, per E3).
                piece["words"] = [dict(w) for w in words[seg_tok_i : seg_tok_i + n_toks]]
            pieces.append(piece)
            seg_tok_i += n_toks

    # Merge short unpunctuated tail fragments into the following cue ("Ona" + "onar…").
    merged: list[dict] = []
    i = 0
    while i < len(pieces):
        p = pieces[i]
        text = str(p["text"]).strip()
        is_fragment = (
            not _SENTENCE_END_RE.search(text) and len(text.split()) <= 2 and i + 1 < len(pieces)
        )
        if is_fragment:
            nxt = pieces[i + 1]
            merged_piece: dict = {
                "text": f"{text} {str(nxt['text']).strip()}",
                "start_s": p["start_s"],
                "end_s": nxt["end_s"],
            }
            # Words survive a merge only when BOTH sides carry them (else the combined
            # list wouldn't spell the text and word-pop would mis-highlight).
            if isinstance(p.get("words"), list) and isinstance(nxt.get("words"), list):
                merged_piece["words"] = list(p["words"]) + list(nxt["words"])
            merged.append(merged_piece)
            i += 2
            continue
        merged.append(p)
        i += 1

    # Monotonic + non-overlapping guarantee (preserves `words` — their times are the
    # real audio times; only the cue window is clamped).
    out: list[dict] = []
    prev_end = 0.0
    for p in merged:
        s = max(float(p["start_s"]), prev_end)
        e = max(s + 0.01, float(p["end_s"]))
        clamped = {
            **p,
            "text": str(p["text"]).strip(),
            "start_s": round(s, 3),
            "end_s": round(e, 3),
        }
        out.append(clamped)
        prev_end = e
    return out


def generate_ass_from_cues(
    cues: list[dict],
    output_path: str,
    *,
    font_name: str | None = None,
    style: str = "plain",
    margin_v: int | None = None,
    pop_in: bool = False,
) -> None:
    """Write a caption ASS from explicit ``{text, start_s, end_s}`` cues.

    The reburn path: after the creator edits caption lines, burn THEIR text (not
    a fresh transcription) at the persisted times. Times are already in the base
    video's (assembled) clock, so no offset is applied.

    ``style``:
      - ``"plain"`` (default) — bottom-third subtitle look (sentence captions).
      - ``"word"`` — one big word centered in the lower-third, the word-by-word
        ("qbuilder") look. Pair with cues from :func:`build_word_cues`.

    ``margin_v`` overrides the vertical margin for the ``plain`` style (e.g. the
    subtitled style passes :data:`SUBTITLED_CAPTION_MARGIN_V` to clear the platform
    UI). None → the narrated default (180). Ignored by the ``word`` style.

    ``pop_in`` (subtitled sentence look): each caption fades in fast and scales
    94%→100%, then hard-cuts to the next cue — prepends :data:`_SENTENCE_POP_TAGS`
    to every line. Off (default) keeps narrated burns byte-identical.
    """
    valid = [c for c in cues if str(c.get("text", "")).strip()]
    if not valid:
        _write_empty_ass(output_path, font_name=font_name, style=style, margin_v=margin_v)
        return
    lines = _format_cue_lines(valid, prefix_tags=_SENTENCE_POP_TAGS if pop_in else "")
    _write_ass(lines, output_path, font_name=font_name, style=style, margin_v=margin_v)


def _words_match_text(words: object, tokens: list[str]) -> bool:
    """True iff a cue's stored per-word list still spells its (possibly edited) text.

    An edit changes `text` but leaves the stale `words` behind, so we can't trust the
    original per-word timings — this exact token match is the E3 test: match ⇒ the cue
    is untouched, reuse real Whisper times; mismatch ⇒ synthesize (never even-split
    real times we still trust).
    """
    if not isinstance(words, list) or len(words) != len(tokens):
        return False
    return [str(w.get("text", "")).strip() for w in words if isinstance(w, dict)] == tokens


def _word_windows_for_cue(cue: dict) -> list[dict]:
    """Resolve a sentence cue to absolute-time per-word windows for the word-pop burn.

    Unedited cue (stored `words` still spell the text) → the real Whisper per-word
    timings, so the highlight stays locked to the audio. Edited/absent → synthesize an
    even split across the cue's own [start_s, end_s] window (E3: only edited cues are
    synthesized). Returns `[{text, start_s, end_s}]` in absolute (base-video) time.
    """
    tokens = str(cue.get("text", "")).split()
    if not tokens:
        return []
    words = cue.get("words")
    if _words_match_text(words, tokens):
        return [
            {"text": str(w["text"]), "start_s": float(w["start_s"]), "end_s": float(w["end_s"])}
            for w in words  # type: ignore[union-attr]
        ]
    from app.pipeline.word_timing import synthesize_word_timings  # noqa: PLC0415

    cs = float(cue.get("start_s", 0.0))
    ce = float(cue.get("end_s", cs))
    # synthesize returns WINDOW-RELATIVE times → offset back to absolute.
    syn = synthesize_word_timings(tokens, 0.0, max(0.01, ce - cs))
    return [
        {"text": s["text"], "start_s": cs + float(s["start_s"]), "end_s": cs + float(s["end_s"])}
        for s in syn
    ]


def _compose_word_pop_text(tokens: list[str], active_idx: int) -> str:
    """The full line, sanitized, with only `tokens[active_idx]` popped in lime (revert
    to the white style default after via `\\r`)."""
    parts: list[str] = []
    for j, tok in enumerate(tokens):
        clean = sanitize_ass_text(tok)
        if j == active_idx:
            parts.append(f"{{\\c{_ACTIVE_WORD_ASS_COLOR}&}}{clean}{{\\r}}")
        else:
            parts.append(clean)
    return " ".join(parts)


def generate_word_pop_ass(
    cues: list[dict],
    output_path: str,
    *,
    font_name: str | None = None,
    margin_v: int | None = None,
) -> None:
    """Word-by-word highlight (D2/E1): the full caption line stays visible and the
    currently-spoken word pops in the Kria lime accent.

    NOT the one-big-word `_ass_header_word` look and NOT the yellow `\\k` karaoke sweep —
    each spoken word gets its own dialogue event that shows the SAME full line (so the
    baseline never jitters) with just that word inline-coloured. Timings come from
    :func:`_word_windows_for_cue` (real when unedited, synthesized per edited cue).
    """
    mv = margin_v if margin_v is not None else SUBTITLED_CAPTION_MARGIN_V
    lines: list[str] = []
    prev_end = 0.0  # monotonic clamp across ALL events (cues included)
    for cue in cues:
        windows = _word_windows_for_cue(cue)
        if not windows:
            continue
        tokens = [w["text"] for w in windows]
        n = len(windows)
        for i, w in enumerate(windows):
            # Clamp against the previous event: whisper word timestamps occasionally
            # regress at segment boundaries, and every word-pop event draws the FULL
            # line — an overlap would stack two complete captions (the lyric-injector
            # stacked-text incident class). Mirrors build_word_cues' guard.
            start = max(0.0, float(w["start_s"]), prev_end)
            nxt = float(windows[i + 1]["start_s"]) if i + 1 < n else float(w["end_s"])
            end = max(start + 0.01, nxt)
            prev_end = end
            text = _compose_word_pop_text(tokens, i)
            lines.append(
                f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{text}"
            )
    _write_ass(lines, output_path, font_name=font_name, style="word_pop", margin_v=mv)


def _write_ass(
    lines: list[str],
    output_path: str,
    *,
    font_name: str | None,
    style: str = "plain",
    margin_v: int | None = None,
) -> None:
    if style == "word":
        header = _ass_header_word(font_name or "TikTok Sans")
    elif style == "word_pop":
        # Line-visible word-by-word: SAME plain geometry (full line at the safe margin);
        # the active word is popped via inline colour in each pre-composed dialogue line,
        # so every event shares one layout (no baseline jitter).
        header = _ass_header_for(
            font_name or "TikTok Sans", margin_v=margin_v or SUBTITLED_CAPTION_MARGIN_V
        )
    elif margin_v is not None:
        # Explicit margin (subtitled safe-zone) — always the font-aware header so the
        # override applies even when font_name is None (default TikTok Sans).
        header = _ass_header_for(font_name or "TikTok Sans", margin_v=margin_v)
    elif font_name is None:
        header = _ASS_HEADER
    else:
        header = _ass_header_for(font_name)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for line in lines:
            f.write(line + "\n")


def _format_cue_lines(cues: list[dict], *, prefix_tags: str = "") -> list[str]:
    """Format ``{text, start_s, end_s}`` cues into ASS Dialogue lines (sanitized).

    ``prefix_tags`` (already-escaped ASS override block, e.g. the sentence pop-in) is
    prepended verbatim to each line's text. Empty (default) → byte-identical output.
    """
    lines: list[str] = []
    for c in cues:
        text = sanitize_ass_text(str(c.get("text", "")).strip())
        if not text:
            continue
        start = max(0.0, float(c["start_s"]))
        end = max(start + 0.01, float(c["end_s"]))
        smart_tags = _SMART_CAPTION_TAGS.get(str(c.get("smart_style") or ""), "")
        lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,"
            f"{prefix_tags}{smart_tags}{text}"
        )
    return lines


def _build_dialogue_lines(words: list[Word], offset_s: float) -> list[str]:
    """Group words into ~5-word chunks; within each chunk highlight word-by-word."""
    CHUNK_SIZE = 5
    dialogue_lines = []

    for i in range(0, len(words), CHUNK_SIZE):
        chunk = words[i : i + CHUNK_SIZE]
        chunk_start = chunk[0].start_s - offset_s
        chunk_end = chunk[-1].end_s - offset_s

        # Build ASS karaoke tags: {\k<centiseconds>}word
        text_parts = []
        for j, word in enumerate(chunk):
            dur_cs = int((word.end_s - word.start_s) * 100)
            clean = sanitize_ass_text(word.text.strip())
            if j == 0:
                text_parts.append(f"{{\\k{dur_cs}}}{clean}")
            else:
                text_parts.append(f" {{\\k{dur_cs}}}{clean}")

        text = "".join(text_parts)
        start_str = format_ass_time(max(0.0, chunk_start))
        end_str = format_ass_time(max(0.0, chunk_end))

        dialogue_lines.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}")

    return dialogue_lines


def _build_plain_lines(words: list[Word], offset_s: float) -> list[str]:
    """Plain line captions (no karaoke). Cues are the editable source of truth, so
    derive the burned lines from the SAME ``build_plain_cues`` chunking."""
    return _format_cue_lines(build_plain_cues(words, offset_s=offset_s))


def _write_empty_ass(
    output_path: str,
    font_name: str | None = None,
    style: str = "plain",
    margin_v: int | None = None,
) -> None:
    _write_ass([], output_path, font_name=font_name, style=style, margin_v=margin_v)


def _ass_caption_header(
    font_name: str, *, fontsize: int, outline: int, shadow: int, margin_v: int
) -> str:
    """Build a 9:16 burn-in caption header around ``font_name``.

    White fill, thick black outline for legibility over arbitrary footage,
    bottom-centered (Alignment 2). The plain vs word-by-word looks differ ONLY in
    fontsize/outline/shadow/marginV — everything else (PlayRes, colours, bold,
    borderstyle, margins) is shared here so the two never drift apart.
    """
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,80,80,{margin_v},1\n"
    )


def _ass_header_for(font_name: str, *, margin_v: int = 180) -> str:
    """Plain subtitle look: lower-third, bottom-centered, held clear of the UI.

    ``margin_v`` defaults to 180 (narrated). The subtitled style passes
    :data:`SUBTITLED_CAPTION_MARGIN_V` so captions clear the platform UI chrome.
    """
    return _ass_caption_header(font_name, fontsize=78, outline=4, shadow=1, margin_v=margin_v)


def _ass_header_word(font_name: str) -> str:
    """Word-by-word ("qbuilder") look: ONE big word (120px) raised to ~73% down the
    frame (MarginV 520) with a heavier outline."""
    return _ass_caption_header(font_name, fontsize=120, outline=6, shadow=2, margin_v=520)


_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding  # noqa: E501
Style: Default,Arial,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,50,50,120,1  # noqa: E501
"""
