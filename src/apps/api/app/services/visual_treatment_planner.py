"""Deterministic guardrails and materialization for visual-treatment output."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from dataclasses import dataclass

from app.agents.visual_treatment_planner import RawVisualTreatment

_CARD_MAX_COVERAGE = 0.18
_SECTION_CARD_MAX_COVERAGE = 0.25
_ALL_MAX_COVERAGE = 0.35
_MAX_SECTION_CARDS = 8
_MAX_ANNOUNCED_SECTION_ITEMS = 50
_MIN_SECTION_FOOTAGE_GAP_S = 0.75
_MAX_SECTION_CARD_DURATION_S = 4.0
_MAX_SECTION_TITLE_TOKENS = 6


def _overlaps(intervals: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(start < old_end and end > old_start for old_start, old_end in intervals)


def _uncovered_footage_duration(
    start: float, end: float, occupied: list[tuple[float, float]]
) -> float:
    """Return talking-head time not replaced by another accepted treatment."""
    if end <= start:
        return 0.0
    covered = sum(
        max(0.0, min(end, old_end) - max(start, old_start)) for old_start, old_end in occupied
    )
    return max(0.0, end - start - covered)


_NUMBER_WORDS = {
    # English cardinals + ordinals.
    "zero": "0",
    "one": "1",
    "first": "1",
    "two": "2",
    "second": "2",
    "three": "3",
    "third": "3",
    "four": "4",
    "fourth": "4",
    "five": "5",
    "fifth": "5",
    "six": "6",
    "sixth": "6",
    "seven": "7",
    "seventh": "7",
    "eight": "8",
    "eighth": "8",
    "nine": "9",
    "ninth": "9",
    "ten": "10",
    "tenth": "10",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    # Turkish cardinals + ordinals. `casefold()` preserves dotted/dotless i,
    # so list every real spelling rather than stripping diacritics.
    "sıfır": "0",
    "bir": "1",
    "ilk": "1",
    "birinci": "1",
    "iki": "2",
    "ikinci": "2",
    "üç": "3",
    "üçüncü": "3",
    "dört": "4",
    "dördüncü": "4",
    "beş": "5",
    "beşinci": "5",
    "altı": "6",
    "altıncı": "6",
    "yedi": "7",
    "yedinci": "7",
    "sekiz": "8",
    "sekizinci": "8",
    "dokuz": "9",
    "dokuzuncu": "9",
    "on": "10",
    "onuncu": "10",
}

_SECTION_ANNOUNCEMENT_WORDS = {
    # English.
    "item",
    "items",
    "list",
    "main",
    "point",
    "points",
    "ranking",
    "rankings",
    "reason",
    "reasons",
    "section",
    "sections",
    "step",
    "steps",
    "topic",
    "topics",
    # Turkish. Keep real spellings because Turkish diacritics are meaningful.
    "adım",
    "adımda",
    "adımlar",
    "ana",
    "başlık",
    "başlıkta",
    "başlıklar",
    "liste",
    "madde",
    "maddede",
    "maddeler",
    "neden",
    "nedenler",
    "sıralama",
    "sıralamada",
    "konu",
    "konular",
}
_SECTION_EXPLANATION_BOUNDARIES = {
    "is",
    "mean",
    "means",
    "meaning",
    "that",
    "which",
    "demek",
    "ise",
    "şudur",
    "yani",
}
_SECTION_MARKER_PREFIXES = {"no", "number", "numara"}
_SECTION_ORDINAL_WORDS = {
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "birinci",
    "ikinci",
    "üçüncü",
    "dördüncü",
    "beşinci",
    "altıncı",
    "yedinci",
    "sekizinci",
    "ilk",
}
_TERMINAL_PUNCTUATION_RE = re.compile(r"[.!?;:]\s*$")
_TIMED_TOKEN_RE = re.compile(
    r"\d+[.),:;!?]?|[^\W_]+(?:[-’'][^\W_]+)*[.,:;!?]?",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class _TimedTranscriptToken:
    raw: str
    normalized: str
    start_s: float
    end_s: float
    sentence_start: bool
    terminal: bool


@dataclass(frozen=True)
class _SectionCandidate:
    ordinal: int
    start_s: float
    end_s: float
    title: str


def _legacy_normalized_tokens(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", value).casefold().replace("%", " percent ")
    legacy_number_words = {
        key: mapped
        for key, mapped in _NUMBER_WORDS.items()
        if key
        in {
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "twenty",
            "thirty",
            "forty",
            "fifty",
            "sixty",
            "seventy",
            "eighty",
            "ninety",
            "hundred",
        }
    }
    return [
        legacy_number_words.get(token, token)
        for token in re.findall(r"\w+", folded, flags=re.UNICODE)
    ]


def _section_normalized_tokens(value: str) -> list[str]:
    folded = (
        unicodedata.normalize("NFKC", value)
        .casefold()
        # Unicode casefold maps Turkish capital İ to `i` + COMBINING DOT,
        # which `\w+` otherwise splits into `i`, `ki`. Remove only that
        # redundant dot; preserve meaningful Turkish diacritics such as ü/ş/ğ.
        .replace("\u0307", "")
        .replace("%", " percent ")
    )
    return [
        _NUMBER_WORDS.get(token, token) for token in re.findall(r"\w+", folded, flags=re.UNICODE)
    ]


def _timed_transcript_tokens(words: list[dict] | None) -> list[_TimedTranscriptToken]:
    """Flatten Whisper words or sentence chunks without inventing copy.

    Some persisted transcripts contain one word per row while eval fixtures and
    legacy jobs contain sentence-sized rows. Sentence rows receive evenly split
    local timings so a short heading does not inherit the whole paragraph span.
    """
    tokens: list[_TimedTranscriptToken] = []
    next_is_sentence_start = True
    for row in words or []:
        if not isinstance(row, dict):
            continue
        value = str(row.get("word") or row.get("text") or "").strip()
        raw_tokens = _TIMED_TOKEN_RE.findall(value)
        if not raw_tokens:
            continue
        try:
            row_start = float(row.get("start_s", 0.0))
            row_end = float(row.get("end_s", row_start))
        except (TypeError, ValueError):
            continue
        row_end = max(row_start, row_end)
        token_duration = (row_end - row_start) / len(raw_tokens) if raw_tokens else 0.0
        for index, raw in enumerate(raw_tokens):
            normalized = _section_normalized_tokens(raw)
            if not normalized:
                continue
            start_s = row_start + token_duration * index
            end_s = row_end if index == len(raw_tokens) - 1 else start_s + token_duration
            terminal = bool(_TERMINAL_PUNCTUATION_RE.search(raw))
            sentence_start = next_is_sentence_start or (
                bool(tokens) and start_s - tokens[-1].end_s >= 0.6
            )
            tokens.append(
                _TimedTranscriptToken(
                    raw=raw,
                    normalized=normalized[0],
                    start_s=start_s,
                    end_s=end_s,
                    sentence_start=sentence_start,
                    terminal=terminal,
                )
            )
            next_is_sentence_start = terminal
    return tokens


def _display_title(tokens: list[_TimedTranscriptToken]) -> str:
    turkish = any(re.search(r"[çğıöşüÇĞİÖŞÜ]", token.raw) for token in tokens)
    words: list[str] = []
    for token in tokens:
        clean = re.sub(r"(^[^\w]+|[^\w’'-]+$)", "", token.raw, flags=re.UNICODE)
        if not clean:
            continue
        if clean.isupper():
            words.append(clean)
        else:
            first = "İ" if turkish and clean[:1] == "i" else clean[:1].upper()
            words.append(first + clean[1:])
    return " ".join(words)


def _raw_word(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("\u0307", "")
    return "".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _section_candidates(tokens: list[_TimedTranscriptToken]) -> list[_SectionCandidate]:
    candidates: list[_SectionCandidate] = []
    index = 0
    while index < len(tokens):
        marker_index = index
        ordinal_index = index
        token = tokens[index]
        if token.normalized in _SECTION_MARKER_PREFIXES and index + 1 < len(tokens):
            ordinal_index = index + 1
        ordinal_token = tokens[ordinal_index]
        try:
            ordinal = int(ordinal_token.normalized)
        except ValueError:
            index += 1
            continue
        if not (1 <= ordinal <= _MAX_ANNOUNCED_SECTION_ITEMS):
            index += 1
            continue
        marker_word = _raw_word(ordinal_token.raw)
        next_token = tokens[ordinal_index + 1] if ordinal_index + 1 < len(tokens) else None
        pause_after_marker = (
            next_token.start_s - ordinal_token.end_s if next_token is not None else 0.0
        )
        explicit_ordinal = marker_word in _SECTION_ORDINAL_WORDS
        numeric_marker = bool(re.fullmatch(r"\d+[.)]?", ordinal_token.raw.strip()))
        if not (
            explicit_ordinal or numeric_marker or token.sentence_start or pause_after_marker >= 0.35
        ):
            index += 1
            continue

        title_tokens: list[_TimedTranscriptToken] = []
        cursor = ordinal_index + 1
        while cursor < len(tokens) and len(title_tokens) < _MAX_SECTION_TITLE_TOKENS:
            title_token = tokens[cursor]
            if title_token.start_s - ordinal_token.start_s > _MAX_SECTION_CARD_DURATION_S:
                break
            if title_tokens and title_token.normalized in _SECTION_EXPLANATION_BOUNDARIES:
                break
            if title_token.sentence_start and title_tokens:
                break
            if title_tokens:
                previous_title = title_tokens[-1]
                gap = title_token.start_s - previous_title.end_s
                previous_duration = previous_title.end_s - previous_title.start_s
                if gap >= 0.12 and previous_duration >= 0.6 and title_token.raw[:1].isupper():
                    break
            title_tokens.append(title_token)
            cursor += 1
            if title_token.terminal:
                break

        if (
            title_tokens
            and title_tokens[0].normalized not in _SECTION_ANNOUNCEMENT_WORDS
            and title_tokens[-1].end_s - tokens[marker_index].start_s
            <= _MAX_SECTION_CARD_DURATION_S
        ):
            title = _display_title(title_tokens)
            if title:
                candidates.append(
                    _SectionCandidate(
                        ordinal=ordinal,
                        start_s=tokens[marker_index].start_s,
                        end_s=title_tokens[-1].end_s,
                        title=title,
                    )
                )
        index = max(index + 1, cursor)
    return candidates


def _announced_item_count(tokens: list[_TimedTranscriptToken], *, before_s: float) -> int | None:
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token.start_s >= before_s or before_s - token.start_s > 20.0:
            continue
        try:
            count = int(token.normalized)
        except ValueError:
            continue
        if not (2 <= count <= _MAX_ANNOUNCED_SECTION_ITEMS):
            continue
        nearby = tokens[index + 1 : index + 5]
        if any(candidate.normalized in _SECTION_ANNOUNCEMENT_WORDS for candidate in nearby):
            return count
    return None


def infer_structured_section_treatments(
    words: list[dict] | None,
) -> list[RawVisualTreatment]:
    """Recover explicit numbered headings when the model misses the structure.

    This detector authors no semantic copy. It only converts a locally timed,
    sequential transcript heading into ``N. Title``. The ordinary materializer
    still enforces transcript grounding, complete-title alignment, spacing,
    overlap, duration, card-count, and global coverage limits.
    """
    tokens = _timed_transcript_tokens(words)
    candidates = _section_candidates(tokens)
    best: list[_SectionCandidate] = []
    for start_index, first in enumerate(candidates):
        for direction in (1, -1):
            run = [first]
            expected = first.ordinal + direction
            for candidate in candidates[start_index + 1 :]:
                if candidate.ordinal != expected:
                    break
                run.append(candidate)
                expected += direction
            announced = _announced_item_count(tokens, before_s=first.start_s)
            required = min(announced or 0, _MAX_SECTION_CARDS)
            announced_match = bool(
                announced
                and len(run) >= required
                and (
                    (direction == 1 and first.ordinal == 1)
                    or (direction == -1 and first.ordinal == announced)
                )
            )
            if announced_match and len(run) > len(best):
                best = run

    return [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=candidate.start_s,
            end_s=candidate.end_s,
            text=f"{candidate.ordinal}. {candidate.title}",
            rationale="Explicit numbered section heading recovered from timed transcript.",
            confidence="high",
        )
        for candidate in best[:_MAX_SECTION_CARDS]
    ]


def _normalized_copy(value: str, *, section_item: bool = False) -> str:
    tokens = _section_normalized_tokens(value) if section_item else _legacy_normalized_tokens(value)
    return " ".join(tokens)


def _card_copy_is_transcript_grounded(
    copy: str, transcript_text: str | None, *, section_item: bool = False
) -> bool:
    """Fail closed: semantic-card tokens must appear in transcript order."""
    if not transcript_text:
        return False
    copy_tokens = _normalized_copy(copy, section_item=section_item).split()
    transcript_tokens = _normalized_copy(transcript_text, section_item=section_item).split()
    if not copy_tokens:
        return False
    cursor = 0
    for token in copy_tokens:
        try:
            cursor = transcript_tokens.index(token, cursor) + 1
        except ValueError:
            return False
    return True


def _card_copy_span_in_window(
    copy: str,
    words: list[dict] | None,
    *,
    start_s: float,
    end_s: float,
    tolerance_s: float = 0.35,
) -> tuple[float, float] | None:
    """Return the local contiguous timed span supporting ``copy``.

    Matching is deliberately restricted to the proposed treatment window. A
    repeated title in a hook or preview therefore cannot pull a later section
    card back to the wrong occurrence. When the same title appears more than
    once inside that local window, prefer the later occurrence so a preview
    cannot beat the subsequently announced item even for a broad model window.
    """
    copy_tokens = _section_normalized_tokens(copy)
    if not copy_tokens or not words:
        return None
    timed_tokens: list[tuple[str, float, float]] = []
    for token in _timed_transcript_tokens(words):
        if token.start_s > end_s + tolerance_s or token.end_s < start_s - tolerance_s:
            continue
        timed_tokens.append((token.normalized, token.start_s, token.end_s))
    matches: list[tuple[float, float]] = []
    width = len(copy_tokens)
    for index in range(0, len(timed_tokens) - width + 1):
        candidate = timed_tokens[index : index + width]
        if [token for token, _start, _end in candidate] == copy_tokens:
            matches.append((candidate[0][1], candidate[-1][2]))
    if not matches:
        return None
    return max(matches, key=lambda span: span[0])


def _transcript_text_in_window(
    words: list[dict] | None,
    *,
    start_s: float,
    end_s: float,
    tolerance_s: float = 0.35,
) -> str | None:
    """Return only timed transcript words overlapping a treatment window."""
    if not words:
        return None
    selected: list[str] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        try:
            word_start = float(word.get("start_s", 0.0))
            word_end = float(word.get("end_s", word_start))
        except (TypeError, ValueError):
            continue
        if word_start <= end_s + tolerance_s and word_end >= start_s - tolerance_s:
            value = str(word.get("word") or word.get("text") or "").strip()
            if value:
                selected.append(value)
    return " ".join(selected) or None


def build_visual_treatments(
    raw: list[RawVisualTreatment],
    *,
    assets_by_id: dict[str, dict],
    duration_s: float,
    transcript_text: str | None = None,
    transcript_words: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return validated-shape blocks + linked TextElements after AI caps."""
    # An explicit spoken structure is a deterministic contract, not a creative
    # model preference. Replace empty, partial, or mistimed model section items
    # with transcript-derived headings before applying the ordinary caps below.
    structured_sections = infer_structured_section_treatments(transcript_words)
    model_sections = sorted(
        (row for row in raw if row.purpose == "section_item"),
        key=lambda row: (row.start_s, row.end_s),
    )
    model_sections_complete = len(model_sections) == len(structured_sections) and all(
        model.confidence != "low"
        and _section_normalized_tokens(model.text or "")[:1]
        == _section_normalized_tokens(inferred.text or "")[:1]
        and _card_copy_span_in_window(
            model.text or "",
            transcript_words,
            start_s=model.start_s,
            end_s=model.end_s,
        )
        is not None
        for model, inferred in zip(model_sections, structured_sections, strict=True)
    )
    if structured_sections and not model_sections_complete:
        section_windows = [(row.start_s, row.end_s) for row in structured_sections]
        raw = [
            row
            for row in raw
            if row.purpose != "section_item"
            and not any(
                row.start_s < section_end and row.end_s > section_start
                for section_start, section_end in section_windows
            )
        ] + structured_sections
    blocks: list[dict] = []
    text_elements: list[dict] = []
    occupied: list[tuple[float, float]] = []
    card_windows: list[tuple[float, float]] = []
    generic_card_windows: list[tuple[float, float]] = []
    section_card_windows: list[tuple[float, float]] = []
    montage_count = 0
    card_limit = min(4, max(1, math.ceil(duration_s / 15.0)))

    for proposal in sorted(raw, key=lambda row: (row.start_s, row.end_s)):
        if proposal.kind == "footage" or proposal.confidence == "low":
            continue
        start = max(0.0, min(duration_s, proposal.start_s))
        end = max(start, min(duration_s, proposal.end_s))

        if proposal.kind == "montage":
            if montage_count >= 2:
                continue
            end = min(end, start + 6.0)
            if end - start < 1.2:
                continue
            if _overlaps(occupied, start, end):
                continue
            if sum(e - s for s, e in occupied) + (end - start) > duration_s * _ALL_MAX_COVERAGE:
                continue
            asset_ids = [asset_id for asset_id in proposal.asset_ids if asset_id in assets_by_id]
            asset_ids = list(dict.fromkeys(asset_ids))[:10]
            if len(asset_ids) < 3:
                continue
            block_id = uuid.uuid4().hex
            shot_duration = (end - start) / len(asset_ids)
            shots: list[dict] = []
            offset = 0.0
            motions = ("zoom_in", "pan_right", "zoom_out", "pan_left")
            for index, asset_id in enumerate(asset_ids):
                asset = assets_by_id[asset_id]
                duration = end - start - offset if index == len(asset_ids) - 1 else shot_duration
                shots.append(
                    {
                        "id": uuid.uuid4().hex,
                        "asset_id": asset_id,
                        "src_gcs_path": asset["gcs_path"],
                        "kind": asset["kind"],
                        "start_offset_s": round(offset, 6),
                        "duration_s": round(duration, 6),
                        "crop": {"x_frac": 0.5, "y_frac": 0.5, "scale": 1.0},
                        "motion": motions[index % len(motions)],
                    }
                )
                offset += duration
            blocks.append(
                {
                    "version": 1,
                    "id": block_id,
                    "kind": "montage",
                    "start_s": start,
                    "end_s": end,
                    "timing_mode": "auto",
                    "origin": "ai",
                    "rationale": proposal.rationale,
                    "transition_in": "cut",
                    "transition_out": "cut",
                    "audio_policy": {"base": "continue", "sfx": "continue"},
                    "shots": shots,
                }
            )
            montage_count += 1
        elif proposal.kind == "text_card":
            is_section = proposal.purpose == "section_item"
            grounded_transcript = (
                _transcript_text_in_window(transcript_words, start_s=start, end_s=end)
                if transcript_words is not None
                else transcript_text
            )
            if (
                (not is_section and len(generic_card_windows) >= card_limit)
                or (is_section and len(section_card_windows) >= _MAX_SECTION_CARDS)
                or not proposal.text
                or not _card_copy_is_transcript_grounded(
                    proposal.text,
                    grounded_transcript,
                    section_item=is_section,
                )
            ):
                continue
            if is_section:
                matched_span = _card_copy_span_in_window(
                    proposal.text,
                    transcript_words,
                    start_s=start,
                    end_s=end,
                )
                if matched_span is None:
                    continue
                start = max(0.0, min(duration_s, matched_span[0]))
                end = max(start, min(duration_s, matched_span[1]))
                if (
                    not math.isfinite(start)
                    or not math.isfinite(end)
                    or end - start < (1.0 / 30.0)
                    or end - start > _MAX_SECTION_CARD_DURATION_S
                ):
                    continue
            else:
                end = min(end, start + 4.0)
            if not is_section and end - start < 0.75:
                continue
            if _overlaps(occupied, start, end):
                continue
            if is_section:
                card_coverage = sum(e - s for s, e in section_card_windows) + (end - start)
                if card_coverage > duration_s * _SECTION_CARD_MAX_COVERAGE:
                    continue
                if (
                    section_card_windows
                    and _uncovered_footage_duration(section_card_windows[-1][1], start, occupied)
                    < _MIN_SECTION_FOOTAGE_GAP_S
                ):
                    continue
            else:
                card_coverage = sum(e - s for s, e in generic_card_windows) + (end - start)
                if card_coverage > duration_s * _CARD_MAX_COVERAGE:
                    continue
                if any(
                    abs(start - prior_end) < 3.0 for _prior_start, prior_end in generic_card_windows
                ):
                    continue
            if sum(e - s for s, e in occupied) + (end - start) > duration_s * _ALL_MAX_COVERAGE:
                continue
            block_id = uuid.uuid4().hex
            blocks.append(
                {
                    "version": 1,
                    "id": block_id,
                    "kind": "text_card",
                    "start_s": start,
                    "end_s": end,
                    "timing_mode": "auto",
                    "origin": "ai",
                    "rationale": proposal.rationale,
                    "transition_in": "cut",
                    "transition_out": "cut",
                    "audio_policy": {"base": "continue", "sfx": "continue"},
                    "style_preset_id": f"nova-{proposal.purpose}",
                    "background": {"type": "solid", "color": "#26382F"},
                }
            )
            text_elements.append(
                {
                    "id": uuid.uuid4().hex,
                    "text": proposal.text[:500],
                    "start_s": start,
                    "end_s": end,
                    "role": "generative_intro",
                    "visual_block_id": block_id,
                    "position": "middle",
                    "font_family": "PlayfairDisplay-Bold",
                    "size_px": 72,
                    "color": "#FFFFFF",
                    "alignment": "center",
                    "effect": "fade-in",
                    "max_width_frac": 0.82,
                }
            )
            card_windows.append((start, end))
            (section_card_windows if is_section else generic_card_windows).append((start, end))
        else:
            continue

        occupied.append((start, end))

    return blocks, text_elements
